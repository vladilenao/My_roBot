"""Разведка стоп-заявок П19–П24 и комиссий П25–П26 на фейковой песочнице.

add-broker-protection, задача 1.3; add-account-vs-emulation-report, задача 1.2.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from t_tech.invest import schemas as s

from src.sandbox import __main__ as command
from src.sandbox.gateway import SandboxGateway
from src.sandbox.probe import NO, SKIPPED, YES, ProbeSettings, ProbeTiming
from src.sandbox.probe_stops import StopProbe
from tests.unit.sandbox.fakes import ACCOUNT_ID, NOW, TOKEN, FakeBroker, FakeClient, robot_account

STOP_CODES = ("П19", "П20", "П21", "П22", "П23", "П24")
FEE_CODES = ("П25", "П26")


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


def _broker(**kwargs) -> FakeBroker:
    broker = FakeBroker(rub=Decimal(2_000_000), **kwargs)
    robot_account(broker)
    return broker


def _probe(broker: FakeBroker, **settings) -> tuple[StopProbe, int]:
    with SandboxGateway(TOKEN, client_factory=lambda token: FakeClient(broker, token),
                        sleep=lambda seconds: None) as gateway:
        probe = StopProbe(gateway, ProbeSettings(**settings), timing=_timing())
        code = probe.run()
    return probe, code


def _outcomes(probe: StopProbe) -> dict[str, str]:
    return {code: result.outcome for code, result in probe.results.items()}


def _active_stops(broker: FakeBroker) -> int:
    return sum(1 for stop in broker.stops.values() if stop.status == s.StopOrderStatusOption.STOP_ORDER_STATUS_ACTIVE)


def test_stop_orders_work_end_to_end():
    broker = _broker(prices=["3.000", "2.990"])
    probe, code = _probe(broker)
    assert code == 0
    assert set(_outcomes(probe).values()) == {YES}, probe.report_lines()
    assert "лотов 2" in probe.results["П19"].detail and probe.results["П19"].detail.endswith(": пункты")
    assert "ошибкой INVALID_ARGUMENT 30057" in probe.results["П20"].detail
    assert "активная стоп-заявка одна" in probe.results["П20"].detail
    trigger = probe.results["П22"].detail
    assert "order_request_id в отчёте пуст" in trigger
    assert "номер отчёта совпадает с exchange_order_id" in trigger
    assert "исполнено 2 из 2, сделок 1" in trigger
    assert "через 10 с после касания" in trigger
    assert "исполнено 2 из 2" in probe.results["П23"].detail
    assert "ошибкой NOT_FOUND 50006" in probe.results["П24"].detail
    assert "статус в GetStopOrders: исполнена" in probe.results["П24"].detail
    assert broker.positions == {} and _active_stops(broker) == 0


def test_stop_price_in_rubles_is_reported():
    broker = _broker(prices=["3.000", "2.990"], stop_price_rub=True)
    probe, _ = _probe(broker)
    assert "рубли за контракт" in probe.results["П19"].detail
    assert probe.results["П22"].outcome == YES   # срабатывание по цене в пунктах


def test_repeated_key_creates_second_stop():
    broker = _broker(prices=["3.000", "2.990"], repeat_stop_key="new")
    probe, _ = _probe(broker)
    assert probe.results["П20"].outcome == NO
    assert "активных стоп-заявок 2, лишние сняты" in probe.results["П20"].detail
    assert probe.results["П21"].outcome == YES
    assert probe.results["П15"].outcome == YES and broker.positions == {}


def test_repeated_key_returns_same_stop():
    broker = _broker(prices=["3.000", "2.990"], repeat_stop_key="same")
    probe, _ = _probe(broker)
    assert probe.results["П20"].outcome == YES
    assert "брокер вернул ту же стоп-заявку" in probe.results["П20"].detail


def test_price_did_not_reach_stop():
    broker = _broker(prices=["3.000"])
    probe, code = _probe(broker, stop_wait_min=1)
    assert code == 0
    assert probe.results["П22"].outcome == SKIPPED
    assert "не дошла за 1 мин" in probe.results["П22"].detail
    for check in ("П23", "П24"):
        assert probe.results[check].outcome == SKIPPED
        assert probe.results[check].detail == "стоп-заявка не исполнена"
    for check in ("П19", "П20", "П21", "П15"):
        assert probe.results[check].outcome == YES
    assert broker.positions == {} and _active_stops(broker) == 0


def test_stop_touched_but_not_executed():
    broker = _broker(prices=["3.000", "2.990"], executes_stops=False)
    probe, _ = _probe(broker)
    assert probe.results["П22"].outcome == NO
    assert "через минуту стоп-заявка активна" in probe.results["П22"].detail
    assert probe.results["П15"].outcome == YES
    assert broker.positions == {} and _active_stops(broker) == 0


def test_trading_closed_places_nothing():
    broker = _broker(trading=s.SecurityTradingStatus.SECURITY_TRADING_STATUS_BREAK_IN_TRADING)
    probe, code = _probe(broker)
    assert code == 1
    for check in ("П5", "П10", *STOP_CODES):
        assert probe.results[check].outcome == SKIPPED
        assert probe.results[check].detail == "торги не идут"
    assert not [name for name, _ in broker.calls
                if name in ("orders.post_order", "stop_orders.post_stop_order")]


def test_market_buy_rejected_skips_stop_checks():
    broker = _broker(reject_market=True)
    probe, _ = _probe(broker)
    assert probe.results["П10"].outcome != YES
    for check in STOP_CODES + FEE_CODES:
        assert probe.results[check].outcome == SKIPPED


def test_probe_stops_command_writes_report_without_secrets(tmp_path):
    broker = _broker(prices=["3.000", "2.990"])
    lines: list[str] = []

    def factory(token, recorder=None):
        return SandboxGateway(token, client_factory=lambda value: FakeClient(broker, value), recorder=recorder)

    code = command.main(["probe-stops", "--stop-wait", "1"], token=TOKEN, gateway_factory=factory,
                        out=lines.append, base_dir=tmp_path, timing=_timing())
    assert code == 0
    runs = list((tmp_path / "sandbox").glob("probe-stops-*"))
    assert len(runs) == 1
    report = (runs[0] / "report.md").read_text(encoding="utf-8")
    records = (runs[0] / "responses.jsonl").read_text(encoding="utf-8")
    assert report.startswith("# Разведка стоп-заявок песочницы: NG-12.26")
    assert "| П22 | Срабатывание и стрим | да |" in report
    assert "лотов 2." in report
    assert lines[0].startswith("Разведка стоп-заявок песочницы: NG")
    for text in (report, records, "\n".join(lines)):
        assert TOKEN not in text
        assert ACCOUNT_ID not in text


def test_probe_stops_rejects_bad_wait(tmp_path):
    lines: list[str] = []
    code = command.main(["probe-stops", "--stop-wait", "0"], token=TOKEN, out=lines.append, base_dir=tmp_path)
    assert code == 2
    assert lines == ["Ошибка параметров: --stop-wait должен быть больше нуля"]


def test_fee_operation_linked_to_buy():
    broker = _broker(prices=["3.000", "2.990"])
    probe, _ = _probe(broker)
    assert probe.results["П25"].outcome == YES
    assert "из них сделок покупки из стрима и stages 1 из 1" in probe.results["П25"].detail
    fee = probe.results["П26"].detail
    assert probe.results["П26"].outcome == YES
    assert "«комиссия брокера» 1.50 ₽ со ссылкой на покупку через 0 с" in fee
    assert "поле commission покупки 1.50 ₽, совпадают" in fee


def test_fee_operation_without_link():
    broker = _broker(prices=["3.000", "2.990"], fee_operation="unlinked")
    probe, _ = _probe(broker)
    assert probe.results["П25"].outcome == YES
    assert probe.results["П26"].outcome == NO
    detail = probe.results["П26"].detail
    assert "со ссылкой на покупку нет за 60 с" in detail
    assert "без ссылки на покупку: 1" in detail
    assert probe.results["П22"].outcome == YES   # стоп-заявки проверяются дальше


def test_operations_failure_does_not_stop_stop_checks():
    broker = _broker(prices=["3.000", "2.990"], fail_operations=True)
    probe, _ = _probe(broker)
    assert probe.results["П25"].outcome not in (YES, SKIPPED)
    assert probe.results["П26"].outcome == SKIPPED and probe.results["П26"].detail
    assert probe.results["П22"].outcome == YES
