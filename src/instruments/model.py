from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    """Нормализованное представление финансового инструмента."""

    label: str
    ticker: str
    instrument_type: str


def normalize_instrument(item) -> Instrument:
    """Приводит 2-кортеж (ticker, type) или 3-кортеж (label, ticker, type) к Instrument."""
    if len(item) == 3:
        label, ticker, instrument_type = item
    elif len(item) == 2:
        ticker, instrument_type = item
        label = f"{ticker} {instrument_type}"
    else:
        raise ValueError(
            f"Некорректный инструмент {item!r}: ожидается 2-кортеж "
            f"(ticker, type) или 3-кортеж (label, ticker, type)"
        )
    return Instrument(label=label, ticker=ticker, instrument_type=instrument_type)
