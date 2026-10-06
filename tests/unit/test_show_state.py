import hashlib
import importlib.util
import json
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
        "VALUES (?, ?, ?, 's1', 'BUY', '{}', '{}', ?, ?, ?, '2', '840')",
        (trade_id, f"assignment:{trade_id}", instrument, phase, created, created),
    )


def test_report_uses_budget_base_as_minimum_of_balance_and_equity(tmp_path):
    path, connection = _database(tmp_path, account=("100000", "98000"))
    connection.commit()
    connection.close()

    report = _report(path)

    assert "База бюджета (минимум баланса и эквити): 98 000,00" in report
    assert "Общий риск портфеля: 0,00 / 1 960,00" in report


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
    assert "контракт не указан" in report


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

    assert limits.portfolio_pct == Decimal(str(RISK_LIMITS["portfolio_pct"]))
    assert limits.commission == Decimal(str(RISK_LIMITS["commission"]))
    assert limits.slippage == Decimal(str(RISK_LIMITS["slippage"]))
    assert not hasattr(limits, "per_trade_pct") and not hasattr(limits, "per_instrument_pct")


def _report(path, limits=None):
    connection = show_state.open_readonly(path)
    try:
        return show_state.build_report(
            connection, show_state.read_names(connection), limits or show_state.load_limits(), NOW, 10
        )
    finally:
        connection.close()


def _position(connection, identifier="t1", *, side="BUY", quantity=3, entry="100", stop="96",
              pending=None, step="1", cost="100", plan=None):
    _add_trade(connection, identifier, phase="OPEN")
    connection.execute("UPDATE trades SET side=?,price_step=?,step_cost=?,plan_json=? WHERE trade_id=?",
                       (side, step, cost, json.dumps(plan or {}), identifier))
    connection.execute("INSERT INTO positions(trade_id,side,quantity,average_price,realized_pnl,fees,updated_at) VALUES(?,?,?,?,'50000','999','2026-09-25')",
                       (identifier, side, quantity, entry))
    connection.execute("INSERT INTO protection(trade_id,confirmed_stop,pending_stop,updated_at) VALUES(?,?,?,'2026-09-25')",
                       (identifier, stop, pending))


def _reservation(connection, identifier="r1", *, risk="500", margin="100", original_risk="1000"):
    connection.execute("INSERT INTO reservations VALUES(?, 't1', ?, ?, ?, ?, ?, 'ACTIVE', '2026-09-25','2026-09-25')",
                       (identifier, identifier, risk, margin, original_risk, margin))


def test_shared_budget_adds_open_and_pending_without_paid_fees_or_profit_discount(tmp_path):
    path, connection = _database(tmp_path, account=("100000", "100000"))
    _position(connection)
    _reservation(connection)
    connection.commit()
    connection.close()
    report = _report(path, show_state.Limits(Decimal(2), Decimal(0), Decimal(0)))
    assert "Общий риск портфеля: 1 700,00 / 2 000,00" in report
    assert "Открытый риск с будущими расходами: 1 200,00" in report
    assert "Незаполненные резервы риска: 500,00" in report
    assert "Свободный риск: 300,00" in report
    assert "Риск на сделку" not in report and "на инструмент" not in report


@pytest.mark.parametrize("side,stop", [("BUY", "105"), ("SELL", "95")])
def test_profitable_confirmed_stop_has_only_future_costs(tmp_path, side, stop):
    path, connection = _database(tmp_path, account=("100000", "100000"))
    _position(connection, side=side, stop=stop, quantity=1)
    connection.commit()
    connection.close()
    report = _report(path)
    assert "Открытый риск с будущими расходами: 2,00" in report
    assert "Свободный риск: 1 998,00" in report
    assert "оценка будущих legacy-затрат" in report


def test_pending_stop_is_separate_and_never_replaces_confirmed_protection(tmp_path):
    path, connection = _database(tmp_path, account=("100000", "100000"))
    _position(connection, quantity=1, pending="100")
    connection.commit()
    connection.close()
    report = _report(path)
    assert "Открытый риск с будущими расходами: 402,00" in report
    assert "100 (ожидается)" in report and "Pending-стоп" in report


@pytest.mark.parametrize("changes", [
    {"stop": None, "pending": "100"}, {"step": None}, {"cost": "0"},
    {"step": "NaN"}, {"plan": {"algorithm_version": "economics-v2"}},
    {"plan": {"cost_snapshot": {"commission": "NaN", "slippage": "1"}}},
])
def test_unknown_exposure_does_not_become_free_budget(tmp_path, changes):
    path, connection = _database(tmp_path)
    _position(connection, **changes)
    connection.commit()
    connection.close()
    report = _report(path)
    assert "risk-state-unknown" in report
    assert "Открытый риск с будущими расходами: неизвестен" in report
    assert "Свободный риск: —" in report
    assert "Новые входы/доборы запрещены" in report


def test_legacy_and_v2_saved_costs_share_budget_and_preserve_source_facts(tmp_path):
    path, connection = _database(tmp_path, account=("100000", "100000"))
    _position(connection, "t1", quantity=1)
    _position(connection, "t2", quantity=1, stop="98", plan={
        "algorithm_version": "economics-v2", "cost_snapshot": {"commission": "3", "slippage": "2"},
        "admission_snapshot": {"portfolio_pct": "100", "go_per_contract": "8000"},
    })
    _reservation(connection)
    connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = _report(path)
    assert "Общий риск портфеля: 1 106,00 / 2 000,00" in report
    assert "Свободный риск: 894,00" in report
    assert "сохранённый снимок расходов" in report and "оценка будущих legacy-затрат" in report
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_partial_fill_uses_actual_remainder_and_remaining_not_original_reserve(tmp_path):
    path, connection = _database(tmp_path, account=("100000", "100000"))
    _position(connection, quantity=1, plan={"admission_snapshot": {"go_per_contract": "100"}})
    _reservation(connection, risk="400", margin="100", original_risk="800")
    connection.commit()
    connection.close()
    report = _report(path, show_state.Limits(Decimal(2), Decimal(0), Decimal(0)))
    assert "Общий риск портфеля: 800,00 / 2 000,00" in report
    assert "Гарантийное обеспечение: 200,00 / 100 000,00" in report


def test_pending_decimal_totals_do_not_use_sqlite_floating_sum(tmp_path):
    _, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="ENTRY_PENDING")
    _reservation(connection, "r1", risk="0.1")
    _reservation(connection, "r2", risk="0.2")
    assert show_state.collect_reservation_totals(connection)[0] == Decimal("0.3")
    connection.close()


@pytest.mark.parametrize("account,percentage,limit,excess", [
    (("100000", "98000"), "2", "1 960,00", "140,00"),
    (("100000", "105000"), "1", "1 000,00", "1 100,00"),
    (("-1", "1000"), "2", "0,00", "2 100,00"),
])
def test_lower_base_or_global_percentage_reports_excess_without_mutation(tmp_path, account, percentage, limit, excess):
    path, connection = _database(tmp_path, account=account)
    _position(connection, quantity=1, stop="79")
    connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = _report(path, show_state.Limits(Decimal(percentage), Decimal(0), Decimal(0)))
    assert f"Общий риск портфеля: 2 100,00 / {limit}" in report
    assert f"Свободный риск: 0,00 ₽ · Превышение: {excess}" in report
    assert "Новые входы/доборы запрещены" in report
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_absent_margin_is_unknown_not_zero(tmp_path):
    path, connection = _database(tmp_path)
    _position(connection, quantity=1)
    connection.commit()
    connection.close()
    report = _report(path)
    assert "Гарантийное обеспечение: —" in report and "неизвестно ГО открытого остатка" in report


def test_unknown_account_does_not_become_zero_capital(tmp_path):
    path, connection = _database(tmp_path, account=("NaN", "98000"))
    connection.commit()
    connection.close()
    report = _report(path)
    assert "risk-state-unknown" in report and "Неизвестны баланс или эквити" in report
    assert "Свободный риск: —" in report


def test_v10_report_refuses_future_schema(tmp_path):
    path, connection = _database(tmp_path)
    connection.execute("PRAGMA user_version=11")
    connection.commit()
    connection.close()
    with pytest.raises(show_state.StateUnavailable):
        show_state.open_readonly(path)


def test_unknown_pending_amount_is_not_a_zero_reservation(tmp_path):
    path, connection = _database(tmp_path)
    _add_trade(connection, "t1", phase="ENTRY_PENDING")
    _reservation(connection, risk="NaN")
    connection.commit()
    connection.close()
    report = _report(path)
    assert "risk-state-unknown" in report and "Незаполненные резервы риска: неизвестны" in report
    assert "Свободный риск: —" in report


def test_report_uses_one_snapshot_even_if_a_writer_changes_account_and_stop(tmp_path, monkeypatch):
    path, writer = _database(tmp_path, account=("100000", "100000"))
    _position(writer, quantity=1)
    writer.commit()
    writer.execute("PRAGMA journal_mode=WAL")
    original = show_state.collect_account

    def read_then_change(connection):
        account = original(connection)
        writer.execute("UPDATE account SET balance='50000',equity='50000'")
        writer.execute("UPDATE protection SET confirmed_stop='98'")
        writer.commit()
        return account

    monkeypatch.setattr(show_state, "collect_account", read_then_change)
    try:
        report = _report(path, show_state.Limits(Decimal(2), Decimal(0), Decimal(0)))
        assert "Общий риск портфеля: 400,00 / 2 000,00" in report
        assert "Свободный риск: 1 600,00" in report
    finally:
        writer.close()
