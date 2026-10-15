"""Буквальные эталоны внешнего вида из OpenSpec change."""

from __future__ import annotations

import io
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from src.events.event import Event
from src.events.types import EventType
from src.notifier.console import ConsoleChannel
from src.notifier.templates import render

SPEC = Path(__file__).resolve().parents[3] / "openspec/specs/notification/spec.md"


def _golden(scenario: str) -> str:
    """Буквальный блок ```text из сценария спеки notification.

    Основная спека — источник после архивации; до неё сценарий читается из
    дельты активного изменения.
    """
    marker = f"#### Scenario: {scenario}\n"
    sources = (SPEC, *sorted(SPEC.parents[2].glob("changes/*/specs/notification/spec.md")))
    text = next(source.read_text() for source in sources if marker in source.read_text())
    section = text.split(marker, 1)[1]
    return section.split("```text\n", 1)[1].split("\n```", 1)[0] + "\n"


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 13, hour, minute, tzinfo=timezone.utc)


def test_full_trading_sequence_matches_approved_text(monkeypatch) -> None:
    monkeypatch.setattr("src.notifier.console.shutil.get_terminal_size", lambda **_: os.terminal_size((120, 24)))
    clock = [_at(11, 5)]
    stream = io.StringIO()
    channel = ConsoleChannel(stream=stream, now=lambda: clock[0])

    def send(event: Event) -> None:
        channel.handle(event)

    send(Event.tick_started(tick_id="scan-66"))
    for index in range(66):
        send(Event.decision(f"PAIR{index}", outcome="no_signal", side="HOLD", timeframe="15m"))
    send(Event.tick_finished(tick_id="scan-66", completed=True))
    send(Event.broker_event(EventType.TARGET_HIT, trade_id="phor", instrument="PHOR",
        target_number=4, quantity=2, price=Decimal("5260.0"), gross_pnl=168,
        net_pnl=Decimal("148.50"), fee=6, fee_source="configured", fees_total=Decimal("19.50"), fees_known=True))

    clock[0] = _at(12, 0)
    send(Event.decision("ED–12.26", outcome="signal_sell", side="SELL", price="1.122",
        timeframe="1h", strategy="macd_rsi_stoch", filter_profile="basic_levels", event_id="ed"))
    send(Event.signal("ED–12.26", side="SELL", quantity=1, entry="1.1224", stop="1.1254",
        targets=("1.1193", "1.1163"), risk_amount="256.44", reward_amount="521.43",
        expected_r="2.03", costs_amount="17.43", diagnostics={"limiting_constraint": "risk"},
        event_id="ed"))

    clock[0] = _at(12, 11)
    send(Event.tick_started(tick_id="scan-3"))
    for index in range(3):
        send(Event.decision(f"PAIR{index}", outcome="no_signal", side="HOLD", timeframe="15m"))
    send(Event.tick_finished(tick_id="scan-3", completed=True))

    clock[0] = _at(12, 15)
    send(Event.decision("MOEX", outcome="signal_sell", side="SELL", price="152.300", timeframe="15m", event_id="moex"))
    send(Event.rejected("MOEX", reason="направление не разрешено для этого типа инструмента",
        code="direction-not-allowed", side="SELL", event_id="moex"))
    send(Event.decision("BR–11.26", outcome="signal_buy", side="BUY", price="102.980", timeframe="5m", event_id="br1"))
    clock[0] = _at(12, 20)
    send(Event.decision("BR–11.26", outcome="signal_buy", side="BUY", price="102.890", timeframe="5m", event_id="br2"))

    clock[0] = _at(12, 37)
    send(Event.broker_event(EventType.TRADE_CLOSED, trade_id="br", instrument="BR–11.26",
        quantity=2, price="103.57", quantity_remaining=0, gross_pnl="-529.98", net_pnl="-535.98",
        fee=3, fee_source="configured", fees_total=6, fees_known=True))
    send(Event.broker_event(EventType.ORDER_REJECTED, trade_id="br", instrument="BR–11.26",
        side="SELL", reason="trade-not-open"))
    send(Event.broker_event(EventType.TARGET_HIT, trade_id="phor", instrument="PHOR",
        target_number=5, quantity=2, price=Decimal("5298.0"), gross_pnl=568,
        net_pnl=541, fee="7.50", fee_source="configured", fees_total=27, fees_known=True))

    clock[0] = _at(13, 3)
    send(Event.heartbeat(tick_count=60, error_count=0))
    clock[0] = _at(13, 15)
    send(Event.decision("VTBR", outcome="signal_buy", side="BUY", price="53.910", timeframe="15m", event_id="vtbr"))
    send(Event.rejected("VTBR", reason="риск до стопа слишком мал относительно издержек",
        code="risk-cost-ratio", diagnostics={"risk_amount": Decimal("3.65")}, side="BUY", event_id="vtbr"))
    send(Event.decision("MTSS", outcome="signal_buy", side="BUY", price="167.150", timeframe="15m", event_id="mtss"))
    send(Event.rejected("MTSS", reason="риск до стопа слишком мал относительно издержек",
        code="risk-cost-ratio", diagnostics={"risk_amount": Decimal("55"), "costs_amount": Decimal("40"),
            "slippage_amount": Decimal("10"), "payoff_ratio": Decimal("1.5"), "threshold": Decimal("2")},
        side="BUY", event_id="mtss"))
    assert stream.getvalue() == _golden("Утверждённый эталон торговых событий")


def test_grouped_data_sequence_matches_approved_text(monkeypatch) -> None:
    monkeypatch.setattr("src.notifier.console.shutil.get_terminal_size", lambda **_: os.terminal_size((120, 24)))
    stream = io.StringIO()
    channel = ConsoleChannel(stream=stream)
    channel.handle(Event.tick_started(tick_id="data"))
    observed = datetime(2026, 10, 13, 13, 15)
    for name, tf, count in (("PHOR", "1h", 42), ("SBER", "15m", 37), ("VTBR", "15m", 51)):
        channel.handle(Event.market_data_status(name, timeframe=tf, session="open",
            data_state="warming_up", observed_at=observed, available_bars=count, required_bars=60))
    for name in ("MOEX", "MTSS"):
        channel.handle(Event.market_data_status(name, timeframe="15m", session="open",
            data_state="coverage_blocked", observed_at=observed,
            reason="Неразрешённый пробел в диапазоне прогрева"))
    for name, tf in (("SBER", "15m"), ("VTBR", "15m"), ("PHOR", "1h")):
        channel.handle(Event.market_data_status(name, timeframe=tf, session="open",
            data_state="delayed", observed_at=observed, expected_bar=datetime(2026, 10, 13, 13, 0)))
    channel.handle(Event.tick_finished(tick_id="data", completed=False))
    assert stream.getvalue() == _golden("Утверждённый эталон одновременных состояний данных")


def test_startup_overview_is_always_silent_and_color_is_text_neutral() -> None:
    for outcome in ("HOLD", "BUY", "SELL", "FILTERED"):
        event = Event.startup_analysis("SBER", published_at=_at(12, 0),
            results=({"outcome": outcome, "reason": "прогрев"},),
            available_bars=1, required_bars=60, coverage_complete=False)
        assert render(event) is None
    decision = Event.decision("SBER", outcome="signal_sell", side="SELL", price=100)
    colored = render(decision, now=_at(12, 0), color=True)
    plain = render(decision, now=_at(12, 0), color=False)
    assert "\x1b[35m" in colored
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == plain


def _stop_moved(name, at, old, new, *, reason, remaining, pnl):
    return Event.broker_event(EventType.STOP_MOVED, trade_id="t", instrument=name, bar_time=at,
        status="ack", reason=reason, previous_stop=old, stop=new, quantity_remaining=remaining,
        pnl_at_stop=pnl, pnl_units="RUB")


def _console(monkeypatch, width: int) -> tuple[ConsoleChannel, io.StringIO]:
    monkeypatch.setattr("src.notifier.console.shutil.get_terminal_size", lambda **_: os.terminal_size((width, 24)))
    stream = io.StringIO()
    return ConsoleChannel(stream=stream, now=lambda: datetime(2026, 10, 15, 11, 5, tzinfo=timezone.utc)), stream


def _at15(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 15, hour, minute, tzinfo=timezone.utc)


def test_stop_move_sequence_matches_approved_text(monkeypatch) -> None:
    channel, stream = _console(monkeypatch, 96)
    channel.handle(Event.broker_event(EventType.TRADE_OPENED, trade_id="t", instrument="ED-12.26", side="BUY",
        bar_time=_at15(9, 1), quantity=4, price="1.1232", fee=6, fee_source="configured",
        gross_pnl=0, net_pnl=-6, fees_total=6, fees_known=True))
    channel.handle(_stop_moved("ED-12.26", _at15(10, 0), "1.1183", "1.1199",
        reason="ma-cloud-protective-stop", remaining=4, pnl="-1108.13604"))
    channel.handle(_stop_moved("ED-12.26", _at15(11, 0), "1.1199", "1.1205",
        reason="ma-cloud-protective-stop", remaining=4, pnl="-906.65676"))
    assert stream.getvalue() == _golden("Утверждённый эталон переноса стопа")


def test_narrow_terminal_keeps_stop_prices_together(monkeypatch) -> None:
    channel, stream = _console(monkeypatch, 60)
    channel.handle(_stop_moved("ED-12.26", _at15(10, 0), "1.1183", "1.1199",
        reason="ma-cloud-protective-stop", remaining=4, pnl="-1108.13604"))
    assert stream.getvalue() == _golden("Узкий терминал не разрывает пару цен")
    assert "\u00a0" not in stream.getvalue()


def test_break_even_move_shows_locked_profit(monkeypatch) -> None:
    channel, stream = _console(monkeypatch, 96)
    channel.handle(_stop_moved("SBER", _at15(11, 30), "296.50", "300.15",
        reason="cost-aware-break-even", remaining=3, pnl="4.5"))
    assert stream.getvalue() == _golden("Безубыток фиксирует прибыль")
