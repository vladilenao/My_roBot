"""Карточки контрактов и пары паттернов: общее для ядра и контура (add-contour-process).

Ядро запрашивает метаданные контрактов через свой арбитр квот и передаёт их
процессу контура файлом запуска; контур к брокеру не обращается. Модуль не
импортирует ни ядро, ни контур.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime

from src.bot.trading_bot import assignments_for
from src.config import FUTURE_STRATEGIES, SHARE_STRATEGIES, TINKOFF_TOKEN
from src.instruments import Instrument, normalize_instrument
from src.instruments.model import instrument_key
from src.logging_setup import get_logger

log = get_logger(__name__)


def _instrument_ticker(instrument) -> str | None:
    """Реальный тикер инструмента: для селектора — второй элемент кортежа (display, ticker, ...)."""
    if isinstance(instrument, (tuple, list)):
        return str(instrument[1]) if len(instrument) > 1 else str(instrument[0])
    return getattr(instrument, "ticker", None)


def _load_contracts_metadata(instruments, client_provider=None, request_budget=None):
    """Кэш метаданных контрактов из источника данных прогона.

    Акции и фьючерсы грузятся по-разному: у фьючерса есть шаг, стоимость шага,
    ГО и дата экспирации, у акции — шаг цены и размер лота (цена лота выводится
    как ``шаг × лот``, ГО отсутствует). Без метаданных входы отклоняются с
    причиной `no-contract-metadata`, поэтому инструменты без метаданных
    перечисляются в лог явно. При сбое — частичный кэш: без него потерян ровно
    тот инструмент, чьи метаданные не прочитались.
    """
    by_type: dict[str, set[str]] = {}
    for instrument in instruments:
        ticker = _instrument_ticker(instrument)
        if not ticker:
            continue
        by_type.setdefault(_instrument_type(instrument), set()).add(ticker)
    if not by_type:
        return {}

    from src.api.instruments import load_futures_contracts, load_share_contracts

    try:
        context = (
            client_provider.client_context() if client_provider is not None else None
        ) or _token_client_context()
    except Exception as exc:
        log.warning("Не удалось открыть клиент источника (%s) — входы отклонятся.", exc)
        return {}
    if context is None:
        log.warning("Нет TINKOFF_TOKEN — метаданные контрактов недоступны.")
        return {}

    contracts: dict[str, object] = {}
    loaders = {"future": load_futures_contracts, "share": load_share_contracts}
    with context as raw_client:
        # Через общий арбитр: каталоги — сервис инструментов, их квота мала.
        client = raw_client if request_budget is None else request_budget.wrap_client(raw_client, lane="interactive")
        for instrument_type, tickers in by_type.items():
            loader = loaders.get(instrument_type)
            if loader is None:
                log.warning(
                    "Метаданные для типа %s не поддерживаются: %s — входы отклонятся.",
                    instrument_type, sorted(tickers),
                )
                continue
            try:
                contracts.update(loader(client, tickers=tickers))
            except Exception as exc:
                log.warning(
                    "Не удалось загрузить метаданные %s (%s) — входы по ним отклонятся.",
                    sorted(tickers), exc,
                )
    known = set(contracts)
    missing = sorted(t for ts in by_type.values() for t in ts if t not in known)
    if missing:
        log.warning("Нет метаданных контрактов для: %s — входы отклонятся.", missing)
    log.info("Метаданные контрактов загружены: %s", sorted(contracts))
    for instrument in instruments:
        if isinstance(instrument, Instrument) and instrument.uid and instrument.ticker in contracts:
            contracts[instrument_key(instrument)] = replace(contracts.pop(instrument.ticker), ticker=instrument_key(instrument))
    return contracts


def _token_client_context():
    """Контекст клиента Тильды; без токена возвращается ``None``."""
    if not TINKOFF_TOKEN:
        return None
    from src.api.client import client_context

    return client_context()


def _instrument_type(instrument) -> str:
    if isinstance(instrument, (tuple, list)):
        return str(instrument[2]) if len(instrument) > 2 else ""
    return str(getattr(instrument, "instrument_type", "") or "")


def _pattern_pairs(instruments) -> tuple:
    """ТФ привязок выбранных инструментов: их закрытые бары ядро ставит воркеру паттернов."""
    pairs = []
    for item in instruments:
        instrument = item if isinstance(item, Instrument) else normalize_instrument(item)
        timeframes = {assignment.timeframe for assignment in
                      assignments_for(instrument, SHARE_STRATEGIES, FUTURE_STRATEGIES)}
        pairs.extend((instrument, timeframe) for timeframe in sorted(timeframes))
    return tuple(pairs)


def instrument_payload(instrument: Instrument) -> dict:
    """Карточка инструмента для файла запуска и строк спроса."""
    return {"label": instrument.label, "ticker": instrument.ticker, "instrument_type": instrument.instrument_type,
            "short_name": instrument.short_name, "uid": instrument.uid, "family": instrument.family}


def instrument_from_payload(payload: dict) -> Instrument:
    return Instrument(payload["label"], payload["ticker"], payload["instrument_type"],
                      short_name=payload.get("short_name"), uid=payload.get("uid"), family=payload.get("family"))


def contracts_payload(contracts) -> dict:
    """``{адрес: ContractMeta}`` → JSON-совместимый словарь (дата экспирации — ISO)."""
    result = {}
    for key, meta in contracts.items():
        item = asdict(meta)
        expiry = item.get("expiration_date")
        item["expiration_date"] = expiry.isoformat() if isinstance(expiry, datetime) else expiry
        result[key] = item
    return result


def contracts_from_payload(payload: dict) -> dict:
    from src.portfolio.models import ContractMeta

    result = {}
    for key, item in payload.items():
        expiry = item.get("expiration_date")
        result[key] = ContractMeta(item["ticker"], float(item["price_step"]), float(item["step_cost"]),
                                   float(item["go_buy"]), float(item["go_sell"]), currency=item.get("currency", "RUB"),
                                   expiration_date=datetime.fromisoformat(expiry) if expiry else None)
    return result
