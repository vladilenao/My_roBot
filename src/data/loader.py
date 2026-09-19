import time

import pandas as pd
from datetime import datetime, timedelta
from t_tech.invest import Client
from t_tech.invest.utils import now
from src.config import TIMEFRAMES
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

log = get_logger(__name__)


def load_candles(
    ticker,
    instrument_type,
    timeframe,
    start_date=None,
    end_date=None,
    token=None,
    instrument_id=None,
):


    """
    Загружает исторические свечи. Полностью повторяет вашу функцию main().
    """
    simple_df = []

    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Неподдерживаемый таймфрейм '{timeframe}'. Доступные: {list(TIMEFRAMES.keys())}")

   # if instrument_type not in ["share", "futures"]:
    #    raise ValueError(f"Неподдерживаемый тип инструмента '{instrument_type}'. Доступные: ['share', 'futures']")

    if start_date is None:
        start_date = now() - timedelta(days=30)
    elif isinstance(start_date, str):
        start_date = datetime.strptime(start_date, '%Y-%m-%d')
    start_date = to_aware_utc(start_date)

    if end_date is None:
        end_date = now()
    elif isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d')
    end_date = to_aware_utc(end_date)

    if start_date >= end_date:
        raise ValueError("Дата начала должна быть меньше даты окончания")

    with Client(token) as client:
        if instrument_id is None:
            instrument_id = find_working_instrument(client, ticker, instrument_type)

        # Итерация по get_all_candles тоже может падать с RESOURCE_EXHAUSTED —
        # api_call_with_retry оборачивает только создание потока. При rate-limit
        # во время итерации ждём и переоткрываем поток с момента последней
        # собранной свечи: прогресс не теряется.
        retries = 0
        candles_from = start_date
        while True:
            try:
                for candle in api_call_with_retry(
                    client.get_all_candles,
                    instrument_id=instrument_id,
                    from_=candles_from,
                    to=end_date,
                    interval=TIMEFRAMES[timeframe],
                ):
                    simple_df.append([
                    candle.time,
                    candle.open.units + candle.open.nano / 1e9,
                    candle.high.units + candle.high.nano / 1e9,
                    candle.low.units + candle.low.nano / 1e9,
                    candle.close.units + candle.close.nano / 1e9,
                    candle.volume,
                    ])
                break
            except Exception as exc:
                if not _is_rate_limited(exc) or retries >= DEFAULT_MAX_RETRIES:
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

    df = pd.DataFrame(simple_df, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = df['datetime'].dt.tz_localize(None)
    return df, instrument_id
