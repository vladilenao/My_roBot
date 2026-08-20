import pandas as pd
from datetime import datetime, timedelta
from t_tech.invest import CandleInterval, Client
from t_tech.invest.utils import now
from src.config import TIMEFRAMES, TINKOFF_TOKEN
from src.api.instruments import find_working_instrument
from src.api.retry import api_call_with_retry


def load_candles(ticker, instrument_type, timeframe, start_date=None, end_date=None, token=None):


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

    if end_date is None:
        end_date = now()
    elif isinstance(end_date, str):
        end_date = datetime.strptime(end_date, '%Y-%m-%d')

    if start_date >= end_date:
        raise ValueError("Дата начала должна быть меньше даты окончания")

    with Client(token) as client:
        instrument_id = find_working_instrument(client, ticker, instrument_type)

        for candle in api_call_with_retry(
            client.get_all_candles,
            instrument_id=instrument_id,
            from_=start_date,
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

    if not simple_df:
        return pd.DataFrame(), instrument_id

    df = pd.DataFrame(simple_df, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = df['datetime'].dt.tz_localize(None)
    return df, instrument_id
