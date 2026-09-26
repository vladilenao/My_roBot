import csv
import json

from src.trade_journal.storage import Storage


def _write_closed_trade(connection, trade_id, instrument, *, price_step=None, step_cost=None):
    started = "2026-09-18T07:00:00+00:00"
    finished = "2026-09-18T07:45:00+00:00"
    token = trade_id.split("-")[-1]
    when = (trade_id, "assignment", instrument, "signal", "BUY",
            json.dumps({"reference_entry": "100", "stop_price": "98"}), "{}", "CLOSED",
            started, finished)
    if price_step is None:
        placeholders = "(?, ?, ?, ?, ?, ?, ?, ?, 0, '{}', ?, ?, NULL, NULL)"
    else:
        placeholders = "(?, ?, ?, ?, ?, ?, ?, ?, 0, '{}', ?, ?, ?, ?)"
        when = when + (price_step, step_cost)
    connection.execute(f"INSERT INTO trades VALUES {placeholders}", when)
    connection.execute(
        "INSERT INTO positions VALUES (?, 'BUY', 0, NULL, '-7', '38', '-45', ?)",
        (trade_id, finished),
    )
    connection.execute(
        "INSERT INTO targets VALUES ('target-1', ?, 0, '104', 2, 2, 'FILLED')",
        (trade_id,),
    )
    fills = (
        (f"fill-{token}-1", f"entry-order-{token}", 5, "100", started, "OPEN"),
        (f"fill-{token}-2", f"add-order-{token}", 3, "101", "2026-09-18T07:15:00+00:00", "ADD"),
        (f"fill-{token}-3", f"tp-order-{token}", 2, "104", "2026-09-18T07:30:00+00:00", "TARGET:target-1"),
        (f"fill-{token}-4", f"stop-order-{token}", 6, "98", finished, "STOP"),
    )
    for fill_id, order_id, quantity, price, executed_at, action in fills:
        command_id = f"cmd-{token}-{order_id}"
        connection.execute(
            "INSERT INTO outbox VALUES (?, ?, '{}', 'SENT', ?, NULL)",
            (command_id, trade_id, executed_at),
        )
        connection.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, 'FILLED', ?, ?, ?, ?, ?)",
            (order_id, trade_id, command_id, action, quantity, quantity, price,
             executed_at, executed_at),
        )
        connection.execute(
            "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, '0', ?)",
            (fill_id, order_id, trade_id, command_id, f"exec-{fill_id}", quantity, price,
             executed_at),
        )
        reason = "TP1" if action.startswith("TARGET") else "STOP" if action == "STOP" else ""
        connection.execute(
            "INSERT INTO events VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)",
            (fill_id, trade_id, order_id, command_id, "FILL",
             json.dumps({"price": price, "reason": reason}), executed_at),
        )
    connection.execute(
        "INSERT INTO reservations VALUES (?, ?, ?, '500', '0', '500', '0', 'RELEASED', ?, ?)",
        (f"reservation-{token}", trade_id, f"entry-order-{token}", started, finished),
    )


def test_trade_summary_keeps_plan_volume_and_exit_sequence(tmp_path):
    database = tmp_path / "trades.sqlite3"
    summary = tmp_path / "trade_summary.csv"
    events = tmp_path / "trade_event.csv"
    with Storage(database, journal_path=events, positions_path=summary) as storage:
        with storage.transaction() as connection:
            connection.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '{}', ?, ?, NULL, NULL)",
                (
                    "BR-10.26-20260918-00143", "assignment", "BRV6", "signal", "BUY",
                    json.dumps({"reference_entry": "100", "stop_price": "98"}),
                    "{}", "CLOSED", "2026-09-18T07:00:00+00:00", "2026-09-18T07:45:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO positions VALUES (?, 'BUY', 0, NULL, '-7', '38', '-45', ?)",
                ("BR-10.26-20260918-00143", "2026-09-18T07:45:00+00:00"),
            )
            connection.execute(
                "INSERT INTO targets VALUES ('target-1', ?, 0, '104', 2, 2, 'FILLED')",
                ("BR-10.26-20260918-00143",),
            )
            fills = (
                ("fill-1", "entry-order", 5, "100", "2026-09-18T07:00:00+00:00", "OPEN"),
                ("fill-2", "add-order", 3, "101", "2026-09-18T07:15:00+00:00", "ADD"),
                ("fill-3", "tp-order", 2, "104", "2026-09-18T07:30:00+00:00", "TARGET:target-1"),
                ("fill-4", "stop-order", 6, "98", "2026-09-18T07:45:00+00:00", "STOP"),
            )
            for fill_id, order_id, quantity, price, executed_at, action in fills:
                connection.execute(
                    "INSERT INTO outbox VALUES (?, ?, '{}', 'SENT', ?, NULL)",
                    (f"command-{order_id}", "BR-10.26-20260918-00143", executed_at),
                )
                connection.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, 'FILLED', ?, ?, ?, ?, ?)",
                    (order_id, "BR-10.26-20260918-00143", f"command-{order_id}", action, quantity, quantity, price, executed_at, executed_at),
                )
                connection.execute(
                    "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, '0', ?)",
                    (fill_id, order_id, "BR-10.26-20260918-00143", f"command-{order_id}", fill_id, quantity, price, executed_at),
                )
                reason = "TP1" if action.startswith("TARGET") else "STOP" if action == "STOP" else ""
                connection.execute(
                    "INSERT INTO events VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (fill_id, "BR-10.26-20260918-00143", order_id, f"command-{order_id}", "FILL", json.dumps({"price": price, "reason": reason}), executed_at),
                )
            connection.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, '500', '0', '500', '0', 'RELEASED', ?, ?)",
                ("reservation", "BR-10.26-20260918-00143", "entry-order", "2026-09-18T07:00:00+00:00", "2026-09-18T07:00:00+00:00"),
            )
    with summary.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert set(row) == {
        "Trade ID", "Контракт", "Направление", "Статус", "Время входа", "Время выхода",
        "Длительность", "План входа", "План стопа", "План TP1", "Начальный объем", "Добрано",
        "Макс. объем", "Средняя входа", "Выходы", "Средняя выхода", "Финальная причина",
        "Сценарий выхода", "Gross PnL", "Комиссия", "Net PnL", "Ед. PnL", "Initial Risk",
        "Result", "MAE", "MFE",
    }
    assert row["Статус"] == "закрыта"
    assert row["Начальный объем"] == "5"
    assert row["Добрано"] == "3"
    assert row["Макс. объем"] == "8"
    assert row["Выходы"] == "TP1: 2 @104.00; STOP: 6 @98.00"
    assert row["Initial Risk"] == "500.00"
    assert row["Ед. PnL"] == "RAW"
    assert row["Комиссия"] == "-38.00"
    assert row["Net PnL"] == "-45.00"
    assert row["Время входа"] == "2026-09-18 10:00:00"
    assert row["Длительность"] == "45 мин"
    with events.open(newline="", encoding="utf-8") as handle:
        event_rows = list(csv.DictReader(handle))
    assert event_rows[0]["Время (МСК)"] == "2026-09-18 10:00:00"
    assert event_rows[0]["Объем позиции до"] == ""
    assert event_rows[0]["Объем позиции после"] == "5"
    assert event_rows[0]["Средняя цена после"] == "100.00"
    assert event_rows[1]["Объем позиции до"] == "5"
    assert event_rows[1]["Объем позиции после"] == "8"
    assert event_rows[2]["Событие"] == "Цель target-1 исполнена"
    assert event_rows[2]["Объем позиции до"] == "8"
    assert event_rows[2]["Объем позиции после"] == "6"
    assert event_rows[3]["Объем позиции после"] == ""


def test_trade_summary_marks_ruble_and_raw_epoch_rows(tmp_path):
    database = tmp_path / "trades.sqlite3"
    summary = tmp_path / "trade_summary.csv"
    events = tmp_path / "trade_event.csv"
    with Storage(database, journal_path=events, positions_path=summary) as storage:
        with storage.transaction() as connection:
            _write_closed_trade(connection, "BR-10.26-20260918-00144", "BRV6")
            _write_closed_trade(
                connection, "BR-10.26-20260918-00145", "BRV6",
                price_step="10", step_cost="8.4",
            )
    with summary.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    by_trade = {row["Trade ID"]: row for row in rows}
    assert len(by_trade) == 2
    assert by_trade["BR-10.26-20260918-00144"]["Ед. PnL"] == "RAW"
    assert by_trade["BR-10.26-20260918-00145"]["Ед. PnL"] == "RUB"
    assert by_trade["BR-10.26-20260918-00145"]["Net PnL"] == "-45.00"
    assert by_trade["BR-10.26-20260918-00145"]["Initial Risk"] == "500.00"
    assert by_trade["BR-10.26-20260918-00145"]["Result"] == "-0.09"
