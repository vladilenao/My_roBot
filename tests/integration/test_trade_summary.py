import csv
import json

from src.trade_journal.storage import Storage


def test_trade_summary_keeps_plan_volume_and_exit_sequence(tmp_path):
    database = tmp_path / "trades.sqlite3"
    summary = tmp_path / "trade_summary.csv"
    events = tmp_path / "trade_event.csv"
    with Storage(database, journal_path=events, positions_path=summary) as storage:
        with storage.transaction() as connection:
            connection.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '{}', ?, ?)",
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
        "Сценарий выхода", "Gross PnL", "Комиссия", "Net PnL", "Initial Risk", "Result", "MAE", "MFE",
    }
    assert row["Статус"] == "закрыта"
    assert row["Начальный объем"] == "5"
    assert row["Добрано"] == "3"
    assert row["Макс. объем"] == "8"
    assert row["Выходы"] == "TP1: 2 @104.00; STOP: 6 @98.00"
    assert row["Initial Risk"] == "500.00"
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
    assert not any("id" in header.lower() for header in event_rows[0])
