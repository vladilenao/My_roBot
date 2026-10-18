"""Подтверждения команд в ленте: причина выхода и перенос стопа без условного объёма."""

import csv
import json

import pytest

from src.trade_journal.storage import Storage


CREATED = "2026-10-10T07:00:00+00:00"
ACK_AT = "2026-10-11T07:00:00+00:00"
APPLIED_AT = "2026-10-11T07:05:00+00:00"


@pytest.fixture
def storage(tmp_path):
    with Storage(
        tmp_path / "trades.sqlite3", journal_path=tmp_path / "trade_event.csv",
        positions_path=tmp_path / "trade_summary.csv",
    ) as store:
        store.set_names({"BRN5": "BR-7.25"})
        yield store


def _events(tmp_path):
    with (tmp_path / "trade_event.csv").open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _trade(connection):
    connection.execute(
        "INSERT INTO trades (trade_id,assignment_id,instrument_id,signal_id,side,plan_json,"
        "profile_json,phase,created_at,updated_at) VALUES ('trade','a','BRN5','signal','BUY',?,'{}','OPEN',?,?)",
        (json.dumps({"reference_entry": "68.05", "stop_price": "67.07"}), CREATED, CREATED),
    )
    connection.execute("INSERT INTO positions (trade_id,side,updated_at) VALUES ('trade','BUY',?)", (CREATED,))


def _command(connection, token, action, command, *, quantity=1, price=None):
    connection.execute(
        "INSERT INTO outbox (command_id,trade_id,payload_json,status,created_at) VALUES (?,'trade',?,'SENT',?)",
        (token, json.dumps(command), CREATED),
    )
    connection.execute(
        "INSERT INTO orders (order_id,trade_id,command_id,action_type,status,quantity,requested_price,"
        "created_at,updated_at) VALUES (?,'trade',?,?,'ACK',?,?,?,?)",
        (token, token, action, quantity, price, CREATED, CREATED),
    )


def _ack(connection, token, event_id, payload, stamp):
    connection.execute(
        "INSERT INTO events (event_id,trade_id,order_id,command_id,event_type,payload_json,occurred_at) "
        "VALUES (?,'trade',?,?,'ACK',?,?)", (event_id, token, token, json.dumps(payload), stamp),
    )


def test_exit_ack_shows_command_reason_instead_of_deferred_entry(storage, tmp_path):
    with storage.transaction() as connection:
        _trade(connection)
        _command(connection, "close", "CLOSE", {"type": "CloseTrade", "reason": "contract-rollover"}, quantity=2)
        _ack(connection, "close", "ack-close", {"reason": "next-bar"}, ACK_AT)
    assert storage.export()
    [row] = _events(tmp_path)
    assert row["Событие"] == "Заявка принята: закрытие позиции"
    assert row["Причина"] == "переход на следующий контракт"


def test_exit_ack_without_command_reason_says_next_bar_execution(storage, tmp_path):
    with storage.transaction() as connection:
        _trade(connection)
        _command(connection, "close", "CLOSE", {"type": "CloseTrade"}, quantity=2)
        _ack(connection, "close", "ack-close", {"reason": "next-bar"}, ACK_AT)
    assert storage.export()
    assert _events(tmp_path)[0]["Причина"] == "исполнение по следующему бару"


def test_stop_move_is_accepted_then_applied_without_order_quantity(storage, tmp_path):
    with storage.transaction() as connection:
        _trade(connection)
        _command(connection, "move", "MOVESTOP", {"type": "MoveStop", "reason": "cost-aware-break-even",
                                                  "stop_price": "63.29"}, price="63.29")
        _ack(connection, "move", "ack-move", {"reason": "next-bar"}, ACK_AT)
        _ack(connection, "move", "ack-move-applied", {
            "reason": "cost-aware-break-even",
            "confirmed_stop": {"effective_at": APPLIED_AT, "old": "62.91", "new": "63.29"},
        }, APPLIED_AT)
    assert storage.export()
    accepted, applied = _events(tmp_path)
    assert (accepted["Событие"], accepted["Количество"], accepted["Цена"], accepted["Причина"]) == (
        "Заявка принята: перенос стопа", "", "63.29", "безубыток всей сделки")
    assert (applied["Событие"], applied["Количество"], applied["Цена"], applied["Причина"]) == (
        "Стоп перенесён", "", "63.29", "безубыток всей сделки: 62.91 → 63.29")


def test_entry_ack_keeps_deferred_entry_reason(storage, tmp_path):
    with storage.transaction() as connection:
        _trade(connection)
        _command(connection, "open", "OPEN", {"type": "OpenTrade", "reason": "profile-entry"}, quantity=2, price="68.05")
        _ack(connection, "open", "ack-open", {"reason": "next-bar"}, ACK_AT)
    assert storage.export()
    [row] = _events(tmp_path)
    assert (row["Событие"], row["Количество"], row["Причина"]) == (
        "Заявка принята: открытие позиции", "2", "вход перенесён на следующий бар")
