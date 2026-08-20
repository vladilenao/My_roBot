from t_tech.invest import InstrumentStatus, CandleInterval
from t_tech.invest.utils import now
from datetime import timedelta
from src.api.retry import api_call_with_retry


def find_working_instrument(client, ticker, instrument_type="share"):
    """
    Ищет UID инструмента по тикеру. (Ваш код из ноутбука)
    """


    if instrument_type == "share":
        response = api_call_with_retry(
            client.instruments.shares, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
    elif instrument_type == "future":
        response = api_call_with_retry(
            client.instruments.futures, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
    elif instrument_type == "etf":2
        response = api_call_with_retry(
            client.instruments.etfs, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
    elif instrument_type == "currency":
        response = api_call_with_retry(
            client.instruments.currencies, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
    else:
        raise ValueError(f"Неподдерживаемый тип инструмента: {instrument_type}")

    for inst in response.instruments:
        if inst.ticker == ticker:
            try:
                test_candles = list(api_call_with_retry(
                    client.get_all_candles,
                    instrument_id=inst.uid,
                    from_=now() - timedelta(days=30),
                    interval=CandleInterval.CANDLE_INTERVAL_DAY,
                ))
                if len(test_candles) > 0:
                    return inst.uid
            except Exception:
                continue
    raise ValueError(f"Инструмент '{ticker}' типа '{instrument_type}' не найден или недоступен")
