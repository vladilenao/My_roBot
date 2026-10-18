import time

import pandas as pd
from datetime import datetime, timedelta
from t_tech.invest.utils import now
from src.config import TIMEFRAMES
from src.api.client import ClientProvider, LIVE_CANDLE_SOURCE, client_context
from src.api.instruments import find_working_instrument
from src.api.retry import (
    DEFAULT_BASE_DELAY,
    DEFAULT_MAX_DELAY,
    DEFAULT_MAX_RETRIES,
    _is_rate_limited,
    api_call_with_retry,
    rate_limit_reset_secs,
)
from src.data.timeutil import to_aware_utc
from src.logging_setup import get_logger
from src.scheduler.clock import Clock, as_clock

log = get_logger(__name__)


def _market_now(clock: Clock | None = None) -> datetime:
    """Текущий рыночный момент: виртуальные часы прогона либо системные часы."""
    return as_clock(clock).now() if clock is not None else now().replace(tzinfo=None)


def _client_context(client_provider: ClientProvider | None = None, token=None):
    """Контекстный менеджер клиента выбранного источника данных."""
    if client_provider is not None:
        return client_provider.client_context(token)
    return client_context(token)


_CATALOG_METHODS = {"share": "shares", "future": "futures", "etf": "etfs", "currency": "currencies"}


def _catalog_instrument_id(client, ticker, instrument_type, *, request_budget=None,
                           wait_for_budget=True, market_now=None):
    """UID инструмента по каталогу: один дешёвый RPC без стрима свечей."""
    if request_budget is None:
        return find_working_instrument(client, ticker, instrument_type, market_now=market_now)
    from t_tech.invest import InstrumentStatus
    method = _CATALOG_METHODS.get(instrument_type)
    if method is None:
        raise ValueError(f"Неподдерживаемый тип инструмента: {instrument_type}")
    if instrument_type not in request_budget.catalogs:
        request_budget.catalogs[instrument_type] = request_budget.call(
            getattr(client.instruments, method),
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE,
            key=("catalog", instrument_type), wait=wait_for_budget).instruments
    card = next((card for card in request_budget.catalogs[instrument_type] if card.ticker == ticker), None)
    if card is None:
        raise ValueError(f"Инструмент {ticker} не найден в каталоге")
    return card.uid


def resolve_instrument_id(*, ticker, instrument_type, token=None, client_provider=None,
                          request_budget=None, wait_for_budget=True, clock=None):
    """Дешёвый UID инструмента (только каталог) для прогрева из локальной базы."""
    market_now = _market_now(clock)
    with _client_context(client_provider, token) as client:
        return _catalog_instrument_id(
            client, ticker, instrument_type, request_budget=request_budget,
            wait_for_budget=wait_for_budget, market_now=market_now,
        )


def load_candles(
    ticker,
    instrument_type,
    timeframe,
    start_date=None,
    end_date=None,
    token=None,
    instrument_id=None,
    client_provider=None,
    clock=None,
    request_budget=None,
    wait_for_budget=True,
    single_attempt=False,
    pace=True,
):


    """
    Загружает исторические свечи. Полностью повторяет вашу функцию main().
    """
    simple_df = []
    actual_sources = {}
    market_now = _market_now(clock)

    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Неподдерживаемый таймфрейм '{timeframe}'. Доступные: {list(TIMEFRAMES.keys())}")

   # if instrument_type not in ["share", "futures"]:
   #    raise ValueError(f"Неподдерживаемый тип инструмента '{instrument_type}'. Доступные: ['share', 'futures']")

    if start_date is None:
        start_date = market_now - timedelta(days=30)
    elif isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d')
    start_date = to_aware_utc(start_date)

    if end_date is None:
        end_date = market_now
    elif isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d')
    end_date = to_aware_utc(end_date)

    if start_date >= end_date:
        raise ValueError("Дата начала должна быть меньше даты окончания")

    with _client_context(client_provider, token) as client:
        if instrument_id is None:
            instrument_id = _catalog_instrument_id(
                client, ticker, instrument_type, request_budget=request_budget,
                wait_for_budget=wait_for_budget, market_now=market_now,
            )

        # Итерация по get_all_candles тоже может падать с RESOURCE_EXHAUSTED —
        # api_call_with_retry оборачивает только создание потока. При rate-limit
        # во время итерации ждём и переоткрываем поток с момента последней
        # собранной свечи: прогресс не теряется.
        retries = 0
        candles_from = start_date
        while True:
            try:
                if request_budget is not None:
                    from t_tech.invest.utils import get_intervals

                    def bounded_candles():
                        seen = set()
                        first_page = pace
                        for lower, upper in get_intervals(TIMEFRAMES[timeframe], candles_from, end_date):
                            response = request_budget.call(
                                client.market_data.get_candles, instrument_id=instrument_id,
                                 from_=lower, to=upper, interval=TIMEFRAMES[timeframe],
                                 candle_source_type=LIVE_CANDLE_SOURCE,
                                key=(instrument_id, timeframe, lower, upper), wait=wait_for_budget,
                                pace=first_page,
                            )
                            first_page = False
                            for candle in response.candles:
                                if candle.time not in seen:
                                    seen.add(candle.time)
                                    yield candle
                    stream = bounded_candles()
                else:
                    stream = api_call_with_retry(
                    client.get_all_candles,
                    instrument_id=instrument_id,
                    from_=candles_from,
                    to=end_date,
                     interval=TIMEFRAMES[timeframe],
                     candle_source_type=LIVE_CANDLE_SOURCE,
                    max_retries=0 if single_attempt else DEFAULT_MAX_RETRIES,
                    )
                for candle in stream:
                    actual_source = getattr(candle, "candle_source", None)
                    if isinstance(actual_source, int) and not isinstance(actual_source, bool):
                        actual_sources[to_aware_utc(candle.time).tz_localize(None).isoformat()] = int(actual_source)
                    simple_df.append([
                    candle.time,
                    candle.open.units + candle.open.nano / 1e9,
                    candle.high.units + candle.high.nano / 1e9,
                    candle.low.units + candle.low.nano / 1e9,
                    candle.close.units + candle.close.nano / 1e9,
                    candle.volume,
                    # Брокер может отдать текущую незавершённую свечу. Она
                    # сохраняется для диагностики, но не должна попасть в
                    # стратегию и экспорт завершённых данных.
                     getattr(candle, "is_complete", None) is True,
                    ])
                break
            except Exception as exc:
                if single_attempt or not _is_rate_limited(exc) or retries >= DEFAULT_MAX_RETRIES:
                    raise
                retries += 1
                reset = rate_limit_reset_secs(exc)
                delay = min(
                    DEFAULT_BASE_DELAY * (2 ** (retries - 1)),
                    reset if reset is not None else DEFAULT_MAX_DELAY,
                    DEFAULT_MAX_DELAY,
                )
                log.warning(
                    "Rate limit при итерации свечей %s (%s). Ожидание %ds...",
                    ticker, timeframe, delay,
                )
                time.sleep(delay)
                candles_from = simple_df[-1][0] if simple_df else start_date

    if not simple_df:
        return pd.DataFrame(), instrument_id

    df = pd.DataFrame(simple_df, columns=['datetime', 'open', 'high', 'low', 'close', 'volume', 'is_complete'])
    df['datetime'] = pd.to_datetime(df['datetime'], utc=True).dt.tz_localize(None)
    df.attrs["actual_candle_sources"] = actual_sources
    return df, instrument_id
