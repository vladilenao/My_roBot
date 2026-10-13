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
    """Буквальный блок ```text из сценария основной спеки notification."""
    section = SPEC.read_text().split(f"#### Scenario: {scenario}\n", 1)[1]
    return section.split("```text\n", 1)[1].split("\n```", 1)[0] + "\n"


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 13, hour, minute, tzinfo=timezone.utc)


def test_full_trading_sequence_matches_approved_text(monkeypatch) -> None:
    monkeypatch.setattr("src.notifier.console.shutil.get_terminal_size", lambda **_: os.terminal_size((120, 24)))
    clock = [_at(11, 5)]
    stream = io.StringIO()
    channel = ConsoleChannel(stream=stream, now=lambda: clock[0], tz_offset_hours=3)

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
    channel = ConsoleChannel(stream=stream, tz_offset_hours=3)
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
