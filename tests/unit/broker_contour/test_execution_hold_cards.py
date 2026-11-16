"""Тревога сторожа исполнения 1m в канале и консоли песочницы (fix-sandbox-restart-safety)."""

from __future__ import annotations

from datetime import datetime, timezone

from src.events.event import Event
from src.notifier.telegram_templates import render_broker_account
from src.notifier.templates import render

HOLD = {"name": "ED-12.26", "since": "2026-11-05T12:00:35+00:00", "minutes": 5,
        "executed": "2026-11-05T12:00:00+00:00", "received": "2026-11-05T12:04:00+00:00"}


def _event(kind):
    return Event.broker_account(kind, contour="sandbox", account="…09c3", event_id="e1", holds=[HOLD])


def test_hold_card_names_instrument_and_says_stop_is_not_checked():
    text = render_broker_account(_event("execution_hold"))

    assert "ТРЕВОГА · Исполнение стоит" in text
    assert "ED-12.26: минутки не исполняются 5 мин" in text and "стоп по инструменту не проверяется" in text


def test_resumed_card_and_console_lines():
    assert "ED-12.26: после 5 мин удержания" in render_broker_account(_event("execution_resumed"))
    now = datetime(2026, 11, 5, 12, 6, tzinfo=timezone.utc)
    assert "Исполнение стоит: ED-12.26 5 мин" in render(_event("execution_hold"), now=now)
    assert "Исполнение возобновлено: ED-12.26 5 мин" in render(_event("execution_resumed"), now=now)
