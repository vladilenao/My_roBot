"""Сводка состоявшихся сделок: факт входа, хронология и пересборка истории."""

import csv
import json

import pytest

from src.trade_journal.export import POSITIONS_HEADERS
from src.trade_journal.storage import Storage


CREATED = "2026-10-10T07:00:00+00:00"
ENTRY = "2026-10-11T07:00:00+00:00"


@pytest.fixture
def storage(tmp_path):
    with Storage(
        tmp_path / "trades.sqlite3", journal_path=tmp_path / "trade_event.csv",
        positions_path=tmp_path / "trade_summary.csv",
    ) as store:
        store.set_names({"SBER": "SBER", "GAZP": "GAZP", "LKOH": "LKOH"})
        yield store


def _rows(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _summary(tmp_path):
    return _rows(tmp_path / "trade_summary.csv")


def _trade(connection, trade_id, *, instrument="SBER", phase="ENTRY_PENDING", created=CREATED):
    connection.execute(
        "INSERT INTO trades (trade_id,assignment_id,instrument_id,signal_id,side,plan_json,"
        "profile_json,phase,created_at,updated_at) VALUES (?,?,?,'signal','BUY',?,'{}',?,?,?)",
        (trade_id, trade_id, instrument, json.dumps({"reference_entry": "100", "stop_price": "98"}),
         phase, created, created),
    )
    connection.execute(
        "INSERT INTO positions (trade_id,side,updated_at) VALUES (?,'BUY',?)", (trade_id, created),
    )


def _order(connection, trade_id, token, *, action="OPEN", status="ACK", quantity=5):
    connection.execute(
        "INSERT INTO outbox (command_id,trade_id,payload_json,status,created_at) "
        "VALUES (?,?,'{}','SENT',?)", (token, trade_id, CREATED),
    )
    connection.execute(
        "INSERT INTO orders (order_id,trade_id,command_id,action_type,status,quantity,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)", (token, trade_id, token, action, status, quantity, CREATED, CREATED),
    )


def _event(connection, trade_id, token, kind, *, order_id=None, stamp=ENTRY):
    connection.execute(
        "INSERT INTO events (event_id,trade_id,order_id,event_type,payload_json,occurred_at) "
        "VALUES (?,?,?,?,'{}',?)", (token, trade_id, order_id, kind, stamp),
    )


def _fill(connection, trade_id, token, stamp=ENTRY, *, action="OPEN", quantity=2):
    _order(connection, trade_id, token, action=action, status="FILLED", quantity=quantity)
    connection.execute("UPDATE orders SET filled_quantity=? WHERE order_id=?", (quantity, token))
    connection.execute(
        "INSERT INTO fills (fill_id,order_id,trade_id,command_id,execution_id,quantity,price,executed_at) "
        "VALUES (?,?,?,?,?,?,'100',?)", (token, token, trade_id, token, token, quantity, stamp),
    )
    _event(connection, trade_id, token, "FILL", order_id=token, stamp=stamp)
    increase = action in {"OPEN", "ADD"}
    connection.execute(
        "UPDATE positions SET quantity=quantity+?,average_price='100',updated_at=? WHERE trade_id=?",
        (quantity if increase else -quantity, stamp, trade_id),
    )
    connection.execute("UPDATE trades SET phase=? WHERE trade_id=?", ("OPEN" if increase else "REDUCING", trade_id))


def _state(connection):
    return {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("trades", "positions", "orders", "fills", "events")
    }


@pytest.mark.parametrize("phase,kind", [
    ("PLANNED", "PLAN"), ("ENTRY_PENDING", "ACK"), ("CANCELLED", "CANCEL"),
    ("REJECTED", "REJECT"), ("ERROR", "REJECT"), ("OPEN", "ACK"),
])
def test_no_card_without_entry_even_with_a_plan_or_accepted_order(storage, tmp_path, phase, kind):
    with storage.transaction() as connection:
        _trade(connection, "trade", phase=phase)
        if phase != "PLANNED":
            _order(connection, "trade", "order", status=kind)
        _event(connection, "trade", "event", kind, order_id=None if phase == "PLANNED" else "order")
    before = _state(storage.connection)
    events_before = _rows(tmp_path / "trade_event.csv")

    assert storage.export()
    assert _summary(tmp_path) == []
    with (tmp_path / "trade_summary.csv").open(newline="", encoding="utf-8") as stream:
        assert list(csv.reader(stream)) == [list(POSITIONS_HEADERS)]
    assert _state(storage.connection) == before
    assert _rows(tmp_path / "trade_event.csv") == events_before
    assert len(events_before) == 1


def test_an_exit_fill_alone_does_not_count_as_an_entry(storage, tmp_path):
    with storage.transaction() as connection:
        _trade(connection, "trade", phase="CLOSED")
        connection.execute("UPDATE positions SET quantity=2 WHERE trade_id='trade'")
        _fill(connection, "trade", "exit", action="CLOSE")
    assert _summary(tmp_path) == []


@pytest.mark.parametrize("exit_quantity,status", [(0, "открыта"), (1, "частично закрыта"), (2, "закрыта")])
def test_entry_appears_and_survives_partial_cancel_and_exits(storage, tmp_path, exit_quantity, status):
    with storage.transaction() as connection:
        _trade(connection, "trade")
        _order(connection, "trade", "entry")
    assert _summary(tmp_path) == []

    with storage.transaction() as connection:
        # A two-lot fill of a five-lot order, followed by cancellation of the rest.
        connection.execute(
            "INSERT INTO fills (fill_id,order_id,trade_id,command_id,execution_id,quantity,price,executed_at) "
            "VALUES ('entry-fill','entry','trade','entry','entry-fill',2,'100',?)", (ENTRY,),
        )
        _event(connection, "trade", "entry-fill", "PARTIAL", order_id="entry")
        connection.execute("UPDATE positions SET quantity=2,average_price='100' WHERE trade_id='trade'")
        connection.execute("UPDATE trades SET phase='OPEN' WHERE trade_id='trade'")
        connection.execute("UPDATE orders SET filled_quantity=2,status='CANCEL' WHERE order_id='entry'")
        _event(connection, "trade", "cancel", "CANCEL", order_id="entry")
    assert len(_summary(tmp_path)) == 1

    if exit_quantity:
        with storage.transaction() as connection:
            _fill(connection, "trade", "exit", "2026-10-11T08:00:00+00:00", action="CLOSE", quantity=exit_quantity)
            if exit_quantity == 2:
                connection.execute("UPDATE trades SET phase='CLOSED' WHERE trade_id='trade'")
    row, = _summary(tmp_path)
    assert row["Статус"] == status
    assert row["Начальный объем"] == "2"
    assert row["Время входа"] == "2026-10-11 10:00:00"
    assert row["Длительность"] == ("60 мин" if exit_quantity == 2 else "")


def test_global_order_follows_entry_instead_of_creation_contract_or_id(storage, tmp_path):
    with storage.transaction() as connection:
        _trade(connection, "a", instrument="GAZP", created="2026-10-09T00:00:00+00:00")
        _trade(connection, "z", instrument="SBER", created="2026-10-10T00:00:00+00:00")
        _trade(connection, "b", instrument="LKOH")
        _fill(connection, "a", "a-fill", "2026-10-11T06:05:00+00:00")
        _fill(connection, "z", "z-fill", "2026-10-10T20:50:00+00:00")
        _fill(connection, "b", "b-fill", "2026-10-11T06:00:00+00:00")
    rows = _summary(tmp_path)
    assert [row["Контракт"] for row in rows] == ["SBER", "LKOH", "GAZP"]
    assert [row["Время входа"] for row in rows] == [
        "2026-10-10 23:50:00", "2026-10-11 09:00:00", "2026-10-11 09:05:00",
    ]


@pytest.mark.parametrize("early,late", [
    ("2026-10-11T10:00:00+03:00", "2026-10-11T07:01:00+00:00"),
    ("2026-10-11T07:00:00.100000+00:00", "2026-10-11T07:00:00.200000+00:00"),
    ("2026-10-11T07:00:00", "2026-10-11T07:00:01+00:00"),
])
def test_order_uses_full_instants_before_display_formatting(storage, tmp_path, early, late):
    with storage.transaction() as connection:
        _trade(connection, "a", instrument="GAZP")
        _trade(connection, "z", instrument="SBER")
        _fill(connection, "a", "late", late)
        _fill(connection, "z", "early", early)
    rows = _summary(tmp_path)
    assert [row["Контракт"] for row in rows] == ["SBER", "GAZP"]
    assert rows[0]["Время входа"] == "2026-10-11 10:00:00"


def test_first_entry_is_selected_by_instant_within_the_trade(storage, tmp_path):
    with storage.transaction() as connection:
        _trade(connection, "trade")
        _fill(connection, "trade", "later", "2026-10-11T07:01:00+00:00", quantity=3)
        _fill(connection, "trade", "earlier", "2026-10-11T10:00:00+03:00", quantity=2)
    row, = _summary(tmp_path)
    assert row["Время входа"] == "2026-10-11 10:00:00"
    assert row["Начальный объем"] == "2"
    assert row["Добрано"] == "3"


def test_ties_and_adds_keep_order_on_repeated_export(storage, tmp_path):
    with storage.transaction() as connection:
        for trade_id, instrument in (("z", "LKOH"), ("b", "GAZP"), ("a", "SBER")):
            _trade(connection, trade_id, instrument=instrument)
            _fill(connection, trade_id, f"entry-{trade_id}")
    before = _summary(tmp_path)
    assert [row["Контракт"] for row in before] == ["SBER", "GAZP", "LKOH"]

    with storage.transaction() as connection:
        _fill(connection, "a", "add", "2026-10-11T08:00:00+00:00", action="ADD")
        _fill(connection, "b", "exit", "2026-10-11T09:00:00+00:00", action="CLOSE")
        connection.execute("UPDATE trades SET phase='CLOSED' WHERE trade_id='b'")
    after = _summary(tmp_path)
    assert [(row["Контракт"], row["Время входа"]) for row in after] == [
        (row["Контракт"], row["Время входа"]) for row in before
    ]
    assert tuple(after[0]) == POSITIONS_HEADERS
    assert storage.export()
    assert _summary(tmp_path) == after


def test_equal_fill_instants_use_fill_id_to_select_initial_quantity(storage, tmp_path):
    with storage.transaction() as connection:
        _trade(connection, "trade")
        _fill(connection, "trade", "z-fill", quantity=3)
        _fill(connection, "trade", "a-fill", "2026-10-11T10:00:00+03:00", quantity=2)
    row, = _summary(tmp_path)
    assert row["Начальный объем"] == "2"
    assert row["Добрано"] == "3"


def test_startup_rebuilds_mixed_history_without_changing_facts(tmp_path):
    database = tmp_path / "trades.sqlite3"
    journal = tmp_path / "trade_event.csv"
    summary = tmp_path / "trade_summary.csv"
    with Storage(database) as store:
        store.set_names({"SBER": "SBER", "GAZP": "GAZP"})
        with store.transaction() as connection:
            _trade(connection, "cancelled", phase="CANCELLED")
            _order(connection, "cancelled", "cancelled-order", status="CANCEL")
            _event(connection, "cancelled", "accepted", "ACK", order_id="cancelled-order")
            _event(connection, "cancelled", "cancel", "CANCEL", order_id="cancelled-order")
            _trade(connection, "a", instrument="GAZP")
            _fill(connection, "a", "late", "2026-10-11T08:00:00+00:00")
            _trade(connection, "z")
            _fill(connection, "z", "early")
        before = _state(store.connection)
    # An old projection, including an unfilled card, must be replaced at startup.
    summary.write_text("Контракт,Статус\nSBER,отменена\nGAZP,открыта\n", encoding="utf-8")
    with Storage(database, journal_path=journal, positions_path=summary) as restarted:
        assert _state(restarted.connection) == before
        assert [row["Контракт"] for row in _summary(tmp_path)] == ["SBER", "GAZP"]
        assert len(_rows(journal)) == 4
        first_export = summary.read_bytes(), journal.read_bytes()
        assert restarted.export()
        assert (summary.read_bytes(), journal.read_bytes()) == first_export
