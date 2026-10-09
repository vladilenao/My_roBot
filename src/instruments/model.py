from __future__ import annotations

from dataclasses import dataclass

SHARE_INSTRUMENT_TYPE = "share"


@dataclass(frozen=True)
class Instrument:
    """Нормализованное представление финансового инструмента."""

    label: str
    ticker: str
    instrument_type: str
    short_name: str | None = None


def normalize_instrument(item) -> Instrument:
    """2-кортеж (ticker, type) | 3-кортеж (label, ticker, type) | 4-кортеж (label, ticker, type, short_name).

    Без короткого имени инструмента ``short_name`` остаётся ``None`` и
    пользователю печатается заглушка: биржевой тикер (`NGV6`/`NGU6`)
    подставлять в текст запрещено правилом именования
    (``openspec/config.yaml``).

    Исключение — акции: у акции нет отдельного биржевого кода контракта, её
    тикер (`SBER`, `GAZP`) и есть короткое имя, поэтому для типа ``share``
    коротким именем по умолчанию становится тикер.
    """
    if len(item) == 4:
        label, ticker, instrument_type, short_name = item
    elif len(item) == 3:
        label, ticker, instrument_type = item
        short_name = None
    elif len(item) == 2:
        ticker, instrument_type = item
        label = f"{ticker} {instrument_type}"
        short_name = None
    else:
        raise ValueError(
            f"Некорректный инструмент {item!r}: ожидается 2-кортеж "
            f"(ticker, type), 3-кортеж (label, ticker, type) "
            f"или 4-кортеж (label, ticker, type, short_name)"
        )
    if not short_name and instrument_type == SHARE_INSTRUMENT_TYPE:
        short_name = ticker
    return Instrument(
        label=label,
        ticker=ticker,
        instrument_type=instrument_type,
        short_name=short_name or None,
    )
