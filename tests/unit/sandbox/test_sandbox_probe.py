"""Разведка песочницы П1–П15 на фейковой песочнице (add-sandbox-probe)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from t_tech.invest import schemas as s

from src.sandbox import __main__ as command
from src.sandbox.gateway import SandboxGateway
from src.sandbox.probe import ERROR, NO, SKIPPED, YES, Probe, ProbeSettings, ProbeTiming
from tests.unit.sandbox.fakes import ACCOUNT_ID, NG_UID, NOW, TOKEN, FakeBroker, FakeClient, robot_account


class FakeTime:
    def __init__(self):
        self.now = NOW

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def _timing() -> ProbeTiming:
    clock = FakeTime()
    return ProbeTiming(clock=clock.clock, sleep=clock.sleep, poll_secs=10, event_wait_secs=0.5,
                       operations_wait_secs=60, stream_ready_secs=2)


def _probe(broker: FakeBroker, **settings) -> tuple[Probe, int]:
    with SandboxGateway(TOKEN, client_factory=lambda token: FakeClient(broker, token)) as gateway:
        probe = Probe(gateway, ProbeSettings(**settings), timing=_timing())
        code = probe.run()
    return probe, code


def _outcomes(probe: Probe) -> dict[str, str]:
    return {code: result.outcome for code, result in probe.results.items()}


def _broker(**kwargs) -> FakeBroker:
    broker = FakeBroker(rub=Decimal(2_000_000), **kwargs)
    robot_account(broker)
    return broker


def _posted(broker: FakeBroker) -> int:
    return sum(1 for name, _ in broker.calls if name in ("orders.post_order", "stop_orders.post_stop_order"))


def test_everything_works_stop_executed():
    broker = _broker(prices=["3.000", "3.000", "2.990"])
    probe, code = _probe(broker)
    assert code == 0
    assert set(_outcomes(probe).values()) == {YES}, probe.report_lines()
    assert "NG → NG-12.26" in probe.results["П3"].detail
    assert "брокер вернул ту же заявку" in probe.results["П8"].detail
    assert broker.positions == {}


def test_stop_touched_but_not_executed():
    broker = _broker(prices=["3.000", "3.000", "2.990"], executes_stops=False)
    probe, code = _probe(broker)
    assert code == 0
    assert probe.results["П14"].outcome == NO
    assert "через минуту стоп-заявка активна" in probe.results["П14"].detail
    assert probe.results["П15"].outcome == YES
    assert broker.positions == {}
    assert all(stop.status != s.StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE for stop in broker.stops.values())


def test_price_did_not_reach_stop():
    broker = _broker(prices=["3.000"])
    probe, _ = _probe(broker, stop_wait_min=1)
    assert probe.results["П14"].outcome == SKIPPED
    assert "не дошла за 1 мин, минимум 3" in probe.results["П14"].detail
    assert probe.results["П15"].outcome == YES


def test_trading_closed_places_nothing():
    broker = _broker(trading=s.SecurityTradingStatus.SECURITY_TRADING_STATUS_BREAK_IN_TRADING)
    probe, code = _probe(broker)
    assert code == 1
    outcomes = _outcomes(probe)
    assert outcomes["П3"] == NO
    for check in [f"П{n}" for n in range(4, 15)]:
        assert outcomes[check] == SKIPPED and probe.results[check].detail == "торги не идут"
    assert outcomes["П15"] == SKIPPED
    assert _posted(broker) == 0


def test_future_market_buy_rejected():
    broker = _broker(reject_market=True)
    probe, code = _probe(broker)
    assert code == 0
    assert probe.results["П10"].outcome == NO
    assert "python -m src.sandbox probe SBER" in probe.results["П10"].detail
    for check in ("П11", "П12", "П13", "П14"):
        assert probe.results[check].outcome == SKIPPED
    assert probe.results["П15"].outcome == YES


def test_failure_after_buy_still_cleans_up():
    broker = _broker(prices=["3.000", "3.000", "2.990"], fail_operations=True)
    probe, _ = _probe(broker)
    assert probe.results["П12"].outcome == ERROR
    assert probe.results["П12"].detail == "UNAVAILABLE"
    assert probe.results["П15"].outcome == YES
    assert broker.positions == {}


def test_repeat_key_creates_second_order():
    broker = _broker(prices=["3.000", "3.000", "2.990"], repeat_key="new")
    probe, _ = _probe(broker)
    assert probe.results["П8"].outcome == NO
    assert "активных заявок 2" in probe.results["П8"].detail
    assert probe.results["П15"].outcome == YES


def test_margin_attributes_unavailable_is_detail():
    broker = _broker(prices=["3.000", "3.000", "2.990"], margin_works=False)
    probe, _ = _probe(broker)
    assert probe.results["П11"].outcome == YES
    assert "GetMarginAttributes не работает: UNIMPLEMENTED 70001" in probe.results["П11"].detail


def test_empty_account_paid_in_to_minimum():
    broker = FakeBroker(prices=["3.000", "3.000", "2.990"])
    probe, _ = _probe(broker)
    assert "открыт сейчас, пополнен на 1 000 000 ₽ до 1 000 000 ₽" in probe.results["П1"].detail
    assert broker.rub == Decimal(1_000_000)


def test_probe_command_writes_report_without_secrets(tmp_path):
    broker = _broker(prices=["3.000", "3.000", "2.990"])
    lines: list[str] = []

    def factory(token, recorder=None):
        return SandboxGateway(token, client_factory=lambda value: FakeClient(broker, value), recorder=recorder)

    code = command.main(["probe", "--stop-wait", "1"], token=TOKEN, gateway_factory=factory, out=lines.append,
                        base_dir=tmp_path, timing=_timing())
    assert code == 0
    runs = list((tmp_path / "sandbox").glob("probe-*"))
    assert len(runs) == 1
    report = (runs[0] / "report.md").read_text(encoding="utf-8")
    records = (runs[0] / "responses.jsonl").read_text(encoding="utf-8")
    log = (tmp_path / "sandbox" / "sandbox.log").read_text(encoding="utf-8")
    assert "| П14 | Стоп-заявка исполняется | да |" in report
    assert lines[-1] == f"Отчёт и записи: {runs[0]}"
    assert "sandbox-account" in records and NG_UID in records
    for text in (report, records, log, "\n".join(lines)):
        assert TOKEN not in text
        assert ACCOUNT_ID not in text
