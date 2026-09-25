import hashlib
import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.trade_journal.schema import initialize_schema

_TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _load_show_state():
    spec = importlib.util.spec_from_file_location("show_state", _TOOLS / "show_state.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["show_state"] = module
    spec.loader.exec_module(module)
    return module


show_state = _load_show_state()

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=show_state.MSK)


def _database(tmp_path, *, version=9, account=("100000", "98000"), names=(("NGV6", "NG-10.26"),)):
    path = tmp_path / "trades.sqlite3"
    connection = sqlite3.connect(path)
    initialize_schema(connection)
    if version < 9:
        connection.executescript("DROP TABLE instrument_names; PRAGMA user_version = 8;")
    connection.execute(
        "INSERT OR REPLACE INTO account VALUES (1, ?, ?, '0', '0', '0', '2026-09-24 12:00:00')",
        account,
    )
    for ticker, short_name in names:
        if version < 9:
            break
        connection.execute(
            "INSERT OR REPLACE INTO instrument_names VALUES (?, ?, '2026-09-25 08:00:00')",
            (ticker, short_name),
        )
    return path, connection


def _add_trade(connection, trade_id, *, phase, instrument="NGV6", created="2026-09-25T09:00:00"):
    connection.execute(
        "INSERT INTO trades (trade_id, assignment_id, instrument_id, signal_id, side, plan_json, "
        "profile_json, phase, created_at, updated_at, price_step, step_cost) "
        "VALUES (?, 'a1', ?, 's1', 'BUY', '{}', '{}', ?, ?, ?, '2', '840')",
        (trade_id, instrument, phase, created, created),
    )


def test_report_uses_budget_base_as_minimum_of_balance_and_equity(tmp_path):
    path, connection = _database(tmp_path, account=("100000", "98000"))
    connection.commit()
    connection.close()

    report = _report(path)

    assert "База бюджета (минимум баланса и эквити): 98 000,00" in report
    assert "Риск на сделку: 0,00 / 1 960,00" in report


def test_report_marks_reservation_of_a_finished_trade_as_orphan(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="CANCELLED", created="2026-09-24T11:00:00")
    connection.execute(
        "INSERT INTO reservations VALUES ('r1', 't1', 'o1', '1588.84173', '21906.72', "
        "'1588.84173', '21906.72', 'ACTIVE', '2026-09-24T11:00:00', '2026-09-24T11:00:00')"
    )
    connection.commit()
    connection.close()

    report = _report(path)

    assert "СИРОТА: сделка CANCELLED" in report
    assert "1 588,84" in report
    assert "21 906,72" in report


def test_report_leaves_a_reservation_of_a_live_trade_unmarked(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="ENTRY_PENDING", created="2026-09-25T11:00:00")
    connection.execute(
        "INSERT INTO reservations VALUES ('r1', 't1', 'o1', '800', '100', '800', '100', "
        "'ACTIVE', '2026-09-25T11:00:00', '2026-09-25T11:00:00')"
    )
    connection.commit()
    connection.close()

    assert "СИРОТА" not in _report(path)


def test_released_reservations_are_not_listed(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="OPEN", created="2026-09-25T11:00:00")
    connection.execute(
        "INSERT INTO reservations VALUES ('r1', 't1', 'o1', '800', '100', '800', '100', "
        "'RELEASED', '2026-09-25T11:00:00', '2026-09-25T11:30:00')"
    )
    connection.commit()
    connection.close()

    assert "АКТИВНЫЕ РЕЗЕРВЫ" in _report(path)
    assert "800" not in _report(path).split("АКТИВНЫЕ РЕЗЕРВЫ")[1].split("ПОСЛЕДНИЕ")[0]


def test_open_trade_shows_quantity_average_stop_and_risk(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="OPEN")
    connection.execute(
        "INSERT INTO positions (trade_id, side, quantity, average_price, updated_at) "
        "VALUES ('t1', 'BUY', 5, '104.00', '2026-09-25T09:00:00')"
    )
    connection.execute(
        "INSERT INTO protection (trade_id, confirmed_stop, updated_at) "
        "VALUES ('t1', '102.00', '2026-09-25T09:00:00')"
    )
    connection.commit()
    connection.close()

    report = _report(path)

    assert "NG-10.26" in report
    assert "ПОКУПКА" in report
    assert "OPEN" in report
    assert "4 200,00" in report


def test_pending_stop_is_marked_as_not_yet_confirmed(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="OPEN")
    connection.execute(
        "INSERT INTO positions (trade_id, side, quantity, average_price, updated_at) "
        "VALUES ('t1', 'BUY', 5, '104.00', '2026-09-25T09:00:00')"
    )
    connection.execute(
        "INSERT INTO protection (trade_id, pending_stop, updated_at) "
        "VALUES ('t1', '102.00', '2026-09-25T09:00:00')"
    )
    connection.commit()
    connection.close()

    assert "102 (ожидается)" in _report(path)


def test_broker_rejections_are_listed_with_a_readable_reason(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="REJECTED")
    connection.execute(
        "INSERT INTO events (event_id, trade_id, command_id, event_type, payload_json, occurred_at) "
        "VALUES ('e1', 't1', NULL, 'REJECT', '{\"reason\": \"risk-or-margin\"}', "
        "'2026-09-25T10:00:00')"
    )
    connection.commit()
    connection.close()

    report = _report(path)

    assert "отклонена" in report
    assert "risk-or-margin" in report


def test_report_never_prints_a_raw_exchange_ticker(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="OPEN")
    connection.execute(
        "INSERT INTO events (event_id, trade_id, command_id, event_type, payload_json, occurred_at) "
        "VALUES ('e1', 't1', NULL, 'REJECT', '{\"reason\": \"stale\"}', '2026-09-25T10:00:00')"
    )
    connection.commit()
    connection.close()

    report = _report(path)

    assert "NGV6" not in report
    assert "NG-10.26" in report


def test_missing_name_map_is_reported_without_falling_back_to_a_ticker(tmp_path):
    path, connection = _database(tmp_path, names=())
    _add_trade(connection, "t1", phase="OPEN")
    connection.commit()
    connection.close()

    report = _report(path)

    assert "Имена контрактов не записаны" in report
    assert "NGV6" not in report
    assert "имя не записано" in report


def test_report_refuses_a_database_of_an_older_schema(tmp_path):
    path, connection = _database(tmp_path, version=8)
    connection.commit()
    connection.close()

    with pytest.raises(show_state.StateUnavailable) as error:
        show_state.open_readonly(path)

    assert "версии 8" in str(error.value)


def test_report_does_not_modify_the_database_file(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="OPEN")
    connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    _report(path)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_report_refuses_a_missing_database(tmp_path):
    with pytest.raises(show_state.StateUnavailable):
        show_state.open_readonly(tmp_path / "absent.sqlite3")


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("2026-09-24T20:15:00", datetime(2026, 9, 24, 20, 15, tzinfo=show_state.MSK)),
        ("2026-09-25T04:00:17+00:00", datetime(2026, 9, 25, 7, 0, 17, tzinfo=show_state.MSK)),
        ("2026-09-25 04:00:17", datetime(2026, 9, 25, 7, 0, 17, tzinfo=show_state.MSK)),
    ],
)
def test_every_stored_time_format_is_read_as_moscow_time(stored, expected):
    assert show_state.parse_timestamp(stored) == expected


def test_unparsable_time_is_marked_instead_of_guessed():
    assert show_state.parse_timestamp("вчера") is None
    assert show_state.parse_timestamp(None) is None
    assert show_state.parse_timestamp("") is None


def test_age_is_reported_in_readable_units():
    assert show_state.format_age(NOW - timedelta(minutes=5), NOW) == "5 мин"
    assert show_state.format_age(NOW - timedelta(hours=3, minutes=7), NOW) == "3 ч 7 мин"
    assert show_state.format_age(NOW - timedelta(days=1, hours=2), NOW) == "1 д 2 ч"
    assert show_state.format_age(None, NOW) == "—"


def test_limits_are_taken_from_configuration():
    from src.config import RISK_LIMITS

    limits = show_state.load_limits()

    assert limits.per_trade_pct == Decimal(str(RISK_LIMITS["trade_pct"]))
    assert limits.per_instrument_pct == Decimal(str(RISK_LIMITS["instrument_pct"]))
    assert limits.portfolio_pct == Decimal(str(RISK_LIMITS["portfolio_pct"]))


def _report(path):
    connection = show_state.open_readonly(path)
    try:
        return show_state.build_report(
            connection, show_state.read_names(connection), show_state.load_limits(), NOW, 10
        )
    finally:
        connection.close()
