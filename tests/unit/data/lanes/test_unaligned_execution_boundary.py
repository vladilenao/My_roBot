"""Граница исполнения 1m после рестарта — по сетке минут (fix-sandbox-restart-safety).

05.11 (песочница, ED-12.26): вход исполнился у брокера в 12:00:04.091 UTC, через 8 с контур убит
и поднят снова. Граница исполнения пришла из времени исполнения, проба «следующей минуты» —
12:01:04.091. Такой строки состояния в хранилище нет, минута считалась неразрешённой, и минутки
ED не исполнялись 8 ч 15 мин: виртуальный стоп не работал, переоценка запрещала входы всем.
"""

import json
from datetime import datetime, timezone

import pandas as pd

from src.trade_journal.storage import Storage
from tests.unit.data.lanes.test_source_absent_execution import _cache, _candles

FILL = "2026-11-05T12:00:04.091405+00:00"


def test_unaligned_boundary_passes_resolved_minute(tmp_path):
    cache, _, item = _cache(tmp_path, state="confirmed_no_trade")
    fresh = _candles("2026-10-13 11:21")

    passed = cache.contiguous_after(item, "1m", fresh, pd.Timestamp("2026-10-13 11:19:04.091405"))

    assert len(passed) == 1


def test_unaligned_boundary_still_holds_unresolved_minute(tmp_path):
    cache, _, item = _cache(tmp_path, state="unresolved")
    fresh = _candles("2026-10-13 11:21")

    assert cache.contiguous_after(item, "1m", fresh, pd.Timestamp("2026-10-13 11:19:04.091405")).empty


def test_unaligned_boundary_finds_source_absent_minute(tmp_path):
    cache, _, item = _cache(tmp_path)
    fresh = _candles("2026-10-13 11:21")

    absent = cache.source_absent_between(item, pd.Timestamp("2026-10-13 11:19:04.091405"), fresh)

    assert [minute for minute, _ in absent] == [pd.Timestamp("2026-10-13 11:20")]


def _trade(storage, trade_id, phase="OPEN"):
    storage.connection.execute(
        "INSERT INTO trades(trade_id,assignment_id,instrument_id,signal_id,side,plan_json,profile_json,phase,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (trade_id, "future-ed-macd", "EDZ6", trade_id, "SELL", "{}", "{}", phase, FILL, FILL))


def test_broker_fill_boundary_is_minute_start(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _trade(storage, "ed")
        storage.connection.execute(
            "INSERT INTO events(event_id,trade_id,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?)",
            ("fill", "ed", "FILL", json.dumps({}), FILL))
        storage.connection.execute(
            "INSERT INTO events(event_id,trade_id,event_type,payload_json,occurred_at) VALUES (?,?,?,?,?)",
            ("stop", "ed", "ACK", json.dumps({"confirmed_stop": "1.1264"}), "2026-11-05T12:00:04.312539+00:00"))

        boundary = storage.execution_bar_boundaries()["EDZ6"]

    assert boundary == (datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc), True)


def test_pending_entry_boundary_is_submission_minute(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _trade(storage, "ed", phase="PENDING")
        storage.connection.execute("INSERT INTO outbox(trade_id,payload_json,status,created_at) VALUES (?,?,?,?)",
                                   ("ed", "{}", "PENDING", "2026-11-05T12:00:02.537000+00:00"))

        boundary = storage.execution_bar_boundaries()["EDZ6"]

    assert boundary == (datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc), True)
