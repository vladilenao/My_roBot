from t_tech.invest import InstrumentStatus, CandleInterval
from t_tech.invest.utils import now
from datetime import timedelta
from src.api.retry import api_call_with_retry
from src.logging_setup import get_logger
from src.portfolio import ContractMeta

log = get_logger(__name__)


def _quotation_to_float(value) -> float:
    """Преобразование Quotation/MoneyValue (units + nano) в число."""
    if value is None:
        return 0.0
    if hasattr(value, "units") and hasattr(value, "nano"):
        return value.units + value.nano / 1e9
    return float(value)


def load_futures_contracts(client, tickers=None) -> dict[str, ContractMeta]:
    """Метаданные фьючерсных контрактов: шаг цены, стоимость шага, ГО.

    Данные берутся из `client.instruments.futures` и пер-тикер `get_futures_margin`.
    При сбое на отдельном контракте он пропускается (входы по нему будут отклонены
    позиционным менеджером с причиной `no-contract-meta`).
    """
    response = api_call_with_retry(
        client.instruments.futures,
        instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE,
    )
    contracts: dict[str, ContractMeta] = {}
    for inst in response.instruments:
        if tickers is not None and inst.ticker not in tickers:
            continue
        try:
            margin = api_call_with_retry(
                client.instruments.get_futures_margin, instrument_id=inst.uid
            )
        except Exception:
            margin = None
        contracts[inst.ticker] = ContractMeta(
            ticker=inst.ticker,
            price_step=_quotation_to_float(
                margin.min_price_increment if margin else inst.min_price_increment
            ),
            step_cost=_quotation_to_float(
                margin.min_price_increment_amount if margin else inst.min_price_increment_amount
            ),
            go_buy=_quotation_to_float(
                margin.initial_margin_on_buy if margin else inst.initial_margin_on_buy
            ),
            go_sell=_quotation_to_float(
                margin.initial_margin_on_sell if margin else inst.initial_margin_on_sell
            ),
        )
    return contracts


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
    elif instrument_type == "etf":
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
