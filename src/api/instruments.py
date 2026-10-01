from t_tech.invest import InstrumentStatus, CandleInterval
from t_tech.invest.utils import now
from datetime import timedelta
from src.api.retry import api_call_with_retry
from src.data.timeutil import to_aware_utc, to_naive
from src.instruments.model import SHARE_INSTRUMENT_TYPE
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
            expiration_date=to_naive(inst.expiration_date),
        )
    return contracts


def load_share_contracts(client, tickers=None) -> dict[str, ContractMeta]:
    """Метаданные акций: шаг цены и размер лота.

    У акции нет гарантийного обеспечения и даты экспирации: цена покупки
    оплачивается деньгами депозита, поэтому ``go_buy``/``go_sell`` равны нулю и
    риск считается в деньгах по геометрии стопа. ``step_cost`` равен шагу,
    умноженному на размер лота, из-за чего ``step_cost / price_step`` даёт цену
    одного лота — на этом считаются объём позиции и прибыль/убыток.
    Контракт без шага цены или размера лота пропускается: входы по нему будут
    отклонены позиционным менеджером с причиной `no-contract-meta`.
    """
    response = api_call_with_retry(
        client.instruments.shares, instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
    )
    contracts: dict[str, ContractMeta] = {}
    for inst in response.instruments:
        if tickers is not None and inst.ticker not in tickers:
            continue
        price_step = _quotation_to_float(inst.min_price_increment)
        lot = int(getattr(inst, "lot", 0) or 0)
        if price_step <= 0 or lot <= 0:
            continue
        contracts[inst.ticker] = ContractMeta(
            ticker=inst.ticker,
            price_step=price_step,
            step_cost=price_step * lot,
            go_buy=0.0,
            go_sell=0.0,
            expiration_date=None,
        )
    return contracts


def instrument_short_name(inst, instrument_type="share") -> str | None:
    """Короткое имя контракта из объекта инструмента API.

    У акции отдельного биржевого кода нет, поэтому коротким именем является её
    тикер. У инструментов с кодом контракта берётся первое слово названия
    (``NG-9.26`` из ``NG-9.26 ...``). Если названия нет, имя неизвестно и
    возвращается ``None``, чтобы вызывающий код показал заглушку, а не тикер.
    """
    if instrument_type == SHARE_INSTRUMENT_TYPE:
        return getattr(inst, "ticker", None) or None
    name = str(getattr(inst, "name", "") or "").strip()
    if not name:
        return None
    return name.split()[0]


def find_working_instrument_with_name(
    client, ticker, instrument_type="share", market_now=None
) -> tuple[str, str | None]:
    """UID рабочего инструмента и его короткое имя.

    Дополнительных запросов не делает: имя выводится из того же ответа
    ``instruments``, по которому инструмент признан рабочим (свечи доступны).
    Короткое имя может быть ``None`` — тогда в пользовательском тексте печатается
    заглушка ``контракт не указан``.

    ``market_now`` — рыночный момент прогона (виртуальные часы истории).
    """
    uid, inst = _find_working(client, ticker, instrument_type, market_now)
    return uid, instrument_short_name(inst, instrument_type)


def find_working_instrument(client, ticker, instrument_type="share", market_now=None) -> str:
    """UID инструмента по тикеру (только идентификатор, без имени).

    ``market_now`` — рыночный момент прогона (виртуальные часы истории);
    без него берётся системное время UTC.
    """
    return _find_working(client, ticker, instrument_type, market_now)[0]


def _find_working(client, ticker, instrument_type, market_now):
    """UID и объект инструмента, если по тикеру идут свечи."""
    probe_from = now() if market_now is None else to_aware_utc(market_now)

    if instrument_type == SHARE_INSTRUMENT_TYPE:
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
                    from_=probe_from - timedelta(days=30),
                    interval=CandleInterval.CANDLE_INTERVAL_DAY,
                ))
                if len(test_candles) > 0:
                    return inst.uid, inst
            except Exception:
                continue
    raise ValueError(f"Инструмент '{ticker}' типа '{instrument_type}' не найден или недоступен")
