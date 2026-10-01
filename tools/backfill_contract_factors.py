"""Восстановление снапшота факторов контракта для старых сделок.

Рублёвые метрики MAE/MFE/R считаются через шаг цены и стоимость шага. Сделки,
заведённые до того как эти поля стали записываться при допуске, остались с
``NULL``, и их прибыль хранится в голых пунктах цены, а не в рублях.

Скрипт дописывает факторы один раз, из метаданных биржи, отдельной операцией:
восстановление намеренно вынесено из обработки событий, потому что редьюсер
обязан воспроизводиться из самого журнала — иначе одна и та же история давала бы
разные деньги в зависимости от того, был ли при обработке доступ к сети.

Запуск:

    .venv/bin/python tools/backfill_contract_factors.py --dry-run
    .venv/bin/python tools/backfill_contract_factors.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run import _token_client_context  # noqa: E402
from src.config import DATABASE_FILE  # noqa: E402
from src.instruments.selector import select_instruments  # noqa: E402


def _plain(value: object) -> str:
    """Decimal as the journal writes it: no exponent, no trailing zeros."""
    return format(Decimal(str(value)).normalize(), "f")


def load_factors(client_context, tickers_by_type: Mapping[str, set[str]]) -> dict[str, tuple[str, str]]:
    """Price step and step cost of the given contracts, by ticker.

    The client context is what talks to the exchange and the tickers are passed
    in rather than selected here, so the caller decides where they come from and
    a test can hand over a stub instead of a token.
    """
    from src.api.instruments import load_futures_contracts, load_share_contracts

    loaders = {"future": load_futures_contracts, "share": load_share_contracts}
    factors: dict[str, tuple[str, str]] = {}
    with client_context() as client:
        for instrument_type, tickers in tickers_by_type.items():
            loader = loaders.get(instrument_type)
            if loader is None:
                continue
            for contract in loader(client, sorted(tickers)).values():
                if contract.price_step and contract.step_cost:
                    factors[contract.ticker] = (
                        _plain(contract.price_step),
                        _plain(contract.step_cost),
                    )
    return factors


def selected_tickers() -> dict[str, set[str]]:
    """Group the instruments the robot trades by exchange section."""
    tickers: dict[str, set[str]] = {}
    for instrument in select_instruments():
        ticker = getattr(instrument, "ticker", None)
        if ticker:
            tickers.setdefault(getattr(instrument, "type", "future"), set()).add(ticker)
    return tickers


def missing_trades(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in connection.execute(
            "SELECT trade_id, instrument_id FROM trades "
            "WHERE price_step IS NULL OR step_cost IS NULL ORDER BY created_at, trade_id"
        )
    ]


def backfill(connection: sqlite3.Connection, factors: Mapping[str, tuple[str, str]]) -> tuple[int, int]:
    """Fill the snapshot from contract metadata and report (updated, skipped)."""
    updated = skipped = 0
    for trade_id, instrument_id in missing_trades(connection):
        found = factors.get(instrument_id)
        if found is None:
            skipped += 1
            continue
        price_step, step_cost = found
        connection.execute(
            "UPDATE trades SET price_step = ?, step_cost = ? WHERE trade_id = ?",
            (price_step, step_cost, trade_id),
        )
        updated += 1
    return updated, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=DATABASE_FILE, help="путь к журналу сделок")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет изменено")
    args = parser.parse_args(argv)

    connection = sqlite3.connect(args.database)
    try:
        pending = missing_trades(connection)
        if not pending:
            print("Недостающих факторов нет.")
            return 0
        factors = load_factors(_token_client_context(), selected_tickers())
        if args.dry_run:
            known = sum(1 for _, instrument in pending if instrument in factors)
            print(
                f"К восстановлению: {len(pending)}; метаданные найдены для {known}; "
                f"без метаданных: {len(pending) - known}"
            )
            return 0
        updated, skipped = backfill(connection, factors)
        connection.commit()
        print(f"Восстановлено: {updated}; пропущено без метаданных: {skipped}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())