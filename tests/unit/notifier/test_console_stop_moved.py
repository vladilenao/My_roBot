"""Строка переноса стопа в консоли: сценарии без буквального эталона (console-stop-moved)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from src.events.event import Event
from src.events.types import EventType
from src.notifier.templates import render

AT = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


def _moved(name="NG-11.26", old="3.012", new="3.025", **fields):
    payload = {"status": "ack", "stop": new, **fields}
    if old is not None:
        payload["previous_stop"] = old
    return Event.broker_event(EventType.STOP_MOVED, trade_id="t", instrument=name, bar_time=AT, **payload)


def _lines(event, **kwargs):
    return render(event, now=AT, width=96, **kwargs).splitlines()


def test_atr_trailing_reason_and_locked_profit():
    event = _moved(reason="atr-trailing-stop", quantity_remaining=2, pnl_at_stop="260", pnl_units="RUB")
    header, detail = _lines(event)
    assert header == "11:00  ЗАЩИТА        NG-11.26 · стоп перенесён 3.012 → 3.025"
    assert detail.strip() == "Трейлинг по ATR · остаток 2 · зафиксировано ~260 ₽ до комиссий"


def test_unknown_reason_and_money_are_omitted_not_invented():
    event = _moved(reason="some-new-code", quantity_remaining=2)
    _, detail = _lines(event)
    assert detail.strip() == "Остаток 2"


def test_zero_result_is_zero_risk_without_estimate_mark():
    event = _moved(reason="atr-trailing-stop", quantity_remaining=2, pnl_at_stop="0", pnl_units="RUB")
    assert _lines(event)[1].strip() == "Трейлинг по ATR · остаток 2 · риск до стопа 0 ₽"


def test_raw_units_do_not_show_rubles():
    event = _moved(reason="atr-trailing-stop", quantity_remaining=2, pnl_at_stop="-13", pnl_units="RAW")
    assert _lines(event)[1].strip() == "Трейлинг по ATR · остаток 2"


def test_unknown_previous_stop():
    event = _moved(name="ED-12.26", old=None, new="1.1199", reason="ma-cloud-protective-stop", quantity_remaining=4)
    assert _lines(event)[0] == "11:00  ЗАЩИТА        ED-12.26 · стоп перенесён на 1.1199"


def test_move_and_stop_execution_use_different_labels_and_roles():
    moved = _moved(name="ED-12.26", old="1.1199", new="1.1205", reason="ma-cloud-protective-stop",
                   quantity_remaining=4, pnl_at_stop="-906.65676", pnl_units="RUB")
    hit = Event.broker_event(EventType.STOP_HIT, trade_id="t", instrument="ED-12.26", bar_time=AT,
                             quantity=4, price="1.1205")
    moved_text, hit_text = render(moved, now=AT, color=True), render(hit, now=AT, color=True)
    assert "ЗАЩИТА" in moved_text and "СТОП " not in moved_text.splitlines()[0]
    assert "\x1b[" not in moved_text
    assert "СТОП" in hit_text.splitlines()[0] and "\x1b[33m" in hit_text


def test_plain_output_equals_colored_and_has_no_nbsp():
    event = _moved(reason="atr-trailing-stop", quantity_remaining=2, pnl_at_stop="260", pnl_units="RUB")
    colored, plain = render(event, now=AT, color=True), render(event, now=AT, color=False)
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == plain
    assert " " not in plain
