"""Восстановление снапшота факторов для сделок, заведённых без них.

Скрипт трогает журнал на записи, поэтому проверяется на копии: важно, что он
дописывает только недостающие значения, не трогает уже записанные и не выдумывает
факторы для контрактов, которых нет в метаданных.
"""

import sqlite3
import sys
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.trade_journal.storage import Storage  # noqa: E402
from tools.backfill_contract_factors import backfill, load_factors, missing_trades  # noqa: E402


NOW = "2026-09-18T07:00:00+00:00"


def _seed(tmp_path, rows):
    database = tmp_path / "trades.sqlite3"
    with Storage(database) as storage:
        with storage.transaction() as connection:
            for trade_id, instrument_id, price_step, step_cost in rows:
                connection.execute(
                    "INSERT INTO trades (trade_id, assignment_id, instrument_id, signal_id, side, "
                    "plan_json, profile_json, phase, state_revision, profile_state_json, created_at, "
                    f"updated_at, price_step, step_cost) VALUES ('{trade_id}', 'a', '{instrument_id}', "
                    f"'s', 'BUY', '{{}}', '{{}}', 'CLOSED', 0, '{{}}', '{NOW}', '{NOW}', "
                    f"{price_step}, {step_cost})"
                )
    return database


@pytest.mark.parametrize(
    "rows, expected",
    [
        ([("t1", "NG", "NULL", "NULL")], [("t1", "NG")]),
        ([("t1", "NG", "'1'", "'100'")], []),
        ([("t1", "NG", "'1'", "NULL")], [("t1", "NG")]),
        ([("t2", "NG", "NULL", "NULL"), ("t1", "SI", "NULL", "NULL")], [("t1", "SI"), ("t2", "NG")]),
    ],
)
def test_only_incomplete_snapshots_are_listed(tmp_path, rows, expected):
    connection = sqlite3.connect(_seed(tmp_path, rows))
    try:
        assert missing_trades(connection) == expected
    finally:
        connection.close()


def test_backfill_fills_only_what_is_missing(tmp_path):
    database = _seed(tmp_path, [
        ("t1", "NG", "NULL", "NULL"),
        ("t2", "SI", "NULL", "NULL"),
        ("t3", "BR", "'10'", "'8.4'"),
    ])
    connection = sqlite3.connect(database)
    try:
        updated, skipped = backfill(connection, {"NG": ("1", "100"), "BR": ("999", "999")})

        assert (updated, skipped) == (1, 1)
        rows = dict(connection.execute("SELECT trade_id, price_step FROM trades"))
        assert rows == {"t1": "1", "t2": None, "t3": "10"}
    finally:
        connection.close()


def test_factors_are_normalised_as_plain_decimals(monkeypatch):
    """Метаданные приходят числами с плавающей точкой; в журнал пишем строкой без хвостов."""
    from src.api import instruments as api

    class Contract:
        def __init__(self, ticker, price_step, step_cost):
            self.ticker, self.price_step, self.step_cost = ticker, price_step, step_cost

    monkeypatch.setattr(
        api, "load_futures_contracts",
        lambda client, tickers: {"NG": Contract("NG", 1.0, 100.0), "SI": Contract("SI", 0.0, 10.0)},
    )

    factors = load_factors(nullcontext, {"future": {"NG", "SI"}, "options": {"XX"}})

    # the unlisted section is ignored, the contract without a step is skipped
    assert factors == {"NG": ("1", "100")}
    assert all(Decimal(value).as_tuple().exponent <= 0 for value in factors["NG"])
