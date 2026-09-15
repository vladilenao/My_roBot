from src.market_context import (
    MarketContextCache,
    SRLevelsCalculator,
    TrendAnalyzer,
)

from src import __version__
from src.decision import RiskManager, SignalFilter
from src.bot import TradingBot
from src.config import (
    ACTIVE_TIMEFRAMES,
    FUTURE_STRATEGIES,
    HEARTBEAT_EVERY_TICKS,
    SHARE_STRATEGIES,
    SLEEP_SECONDS,
    TICKER,
    TICK_POLL_SECS,
    TICK_TIMEOUT_SECS,
    TINKOFF_TOKEN,
    TRIPLE_SCREEN_PARAMS,
    INSTRUMENT_TYPE,
    LOGGING_SERVICE_UID,
    LOGGING_FILE,
    LOGGING_LEVEL,
    LOGGING_MAX_BYTES,
    LOGGING_BACKUP_COUNT,
    CLEARING_TIMES,
    INITIAL_DEPOSIT,
    JOURNAL_FILE,
    MAX_RISK_PCT,
    POSITIONS_FILE,
    trading_enabled,
)
from src.data.cache import MarketDataCache
from src.data.htf_provider import HtfFrameProvider
from src.data.loader import load_candles
from src.decision.filters import PROFILES
from src.decision.filters.triple_screen import TripleScreenFilter
from src.execution import NotifyOnlyExecutionPort
from src.instruments.selector import select_instruments
from src.logging_setup import get_logger, setup_logging
from src.notifier import get_notifier
from src.scheduler.timing import MultiTimeframeScheduler

log = get_logger(__name__)


def main():
    setup_logging(
        service_uid=LOGGING_SERVICE_UID,
        log_file=LOGGING_FILE,
        level=LOGGING_LEVEL,
        max_bytes=LOGGING_MAX_BYTES,
        backup_count=LOGGING_BACKUP_COUNT,
    )
    log.info("Робот v%s запущен", __version__)
    instruments = select_instruments() or [(TICKER, TICKER, INSTRUMENT_TYPE)]
    notifier = get_notifier()
    timeline = MultiTimeframeScheduler(
        timeframes=sorted(ACTIVE_TIMEFRAMES), sleep_secs=SLEEP_SECONDS
    )
    data_cache = MarketDataCache(
        loader=load_candles, timeline=timeline, token=TINKOFF_TOKEN
    )

    htf_provider = HtfFrameProvider(cache=data_cache, timeline=timeline)
    PROFILES["triple_screen"] = TripleScreenFilter(
        provider=htf_provider, params=TRIPLE_SCREEN_PARAMS
    )

    execution, post_tick = _build_execution(instruments, notifier, data_cache)

    TradingBot(
        instruments=instruments,
        notifier=notifier,
        strategy_map=_strategy_map(),
        data_cache=data_cache,
        timeline=timeline,
        execution=execution,
        share_strategies=SHARE_STRATEGIES,
        future_strategies=FUTURE_STRATEGIES,
        heartbeat_every_ticks=HEARTBEAT_EVERY_TICKS,
        tick_poll_secs=TICK_POLL_SECS,
        tick_timeout_secs=TICK_TIMEOUT_SECS,
        context_cache=MarketContextCache(
            data_cache=data_cache,
            trend_analyzer=TrendAnalyzer(),
            sr_calculator=SRLevelsCalculator(),
        ),
        signal_filter=SignalFilter(),
        risk_manager=RiskManager(),
        post_tick=post_tick,
    ).run()


def _build_execution(instruments, notifier, data_cache):
    """Выбор порта исполнения: имитация через журнал сделок или NotifyOnly.

    Торговый режим активен при наличии секции `[trading]` в конфиге; метаданные
    контрактов для расчёта маржи/объёма подгружаются из API Тильды (если есть
    токен), иначе — пустой кэш, и исполнение планово отклоняет входы с причиной
    `no-contract-meta`.
    """
    if not trading_enabled():
        log.info("Торговый режим выключен — NotifyOnly.")
        return NotifyOnlyExecutionPort(notifier), None

    from datetime import UTC, datetime

    from src.broker import create_journal_broker
    from src.broker.exec_adapter import BrokerExecutionAdapter

    try:
        broker = create_journal_broker(
            JOURNAL_FILE,
            INITIAL_DEPOSIT,
            MAX_RISK_PCT,
            CLEARING_TIMES,
            positions_file=POSITIONS_FILE,
            contract_names=_instrument_names(instruments),
        )
    except Exception as exc:
        log.warning("Не удалось поднять журнал сделок (%s) — NotifyOnly.", exc)
        return NotifyOnlyExecutionPort(notifier), None

    from src.execution import BrokerExecutionPort

    adapter = BrokerExecutionAdapter(broker, broker.manager)
    contracts = _load_contracts_metadata(instruments)
    adapter.set_contracts(contracts, names=_instrument_names(instruments))
    print_contract_metadata(contracts)

    def on_bar(ready_tfs: set[str]) -> None:
        try:
            now = datetime.now(UTC).replace(microsecond=0)
            broker.run_clearing_if_due(now)
            prices = {}
            for instrument in instruments:
                for tf in ready_tfs:
                    try:
                        frame = data_cache.frame_for(instrument, tf)
                    except Exception:
                        continue
                    if frame.empty:
                        continue
                    prices[instrument.ticker] = (
                        float(frame["low"].iloc[-1]),
                        float(frame["high"].iloc[-1]),
                        float(frame["close"].iloc[-1]),
                    )
            if prices:
                broker.track_bar(now, prices, adapter._contracts)
            for event in broker.drain_events():
                notifier.notify_event(event.type, event.position_id, event.message)
        except Exception as exc:
            log.warning("Сбой обработки бара исполнением: %s", exc)

    return BrokerExecutionPort(adapter, notifier), on_bar


def _instrument_ticker(instrument) -> str | None:
    """Реальный тикер инструмента: для селектора — второй элемент кортежа (display, ticker, ...)."""
    if isinstance(instrument, (tuple, list)):
        return str(instrument[1]) if len(instrument) > 1 else str(instrument[0])
    return getattr(instrument, "ticker", None)


def _instrument_names(instruments) -> dict[str, str]:
    """Карта тикер -> короткое имя (NG-10.26) из селектора/нормализации инструментов."""
    names: dict[str, str] = {}
    for instrument in instruments:
        ticker = _instrument_ticker(instrument)
        if not ticker:
            continue
        if isinstance(instrument, (tuple, list)):
            short = instrument[3] if len(instrument) > 3 else None
        else:
            short = getattr(instrument, "short_name", None) or getattr(instrument, "label", None)
        names[ticker] = short or ticker
    return names


def _load_contracts_metadata(instruments):
    """Кэш метаданных контрактов из API Тильды; при сбое — пустой кэш (входы отклоняются)."""
    if not TINKOFF_TOKEN:
        log.warning("Нет TINKOFF_TOKEN — метаданные контрактов недоступны.")
        return {}
    tickers = {t for t in (_instrument_ticker(i) for i in instruments) if t}
    if not tickers:
        return {}
    try:
        from src.api.client import client_context
        from src.api.instruments import load_futures_contracts

        with client_context() as client:
            contracts = load_futures_contracts(client, tickers=tickers)
        log.info("Метаданные контрактов загружены: %s", sorted(contracts))
        return contracts
    except Exception as exc:
        log.warning("Не удалось загрузить метаданные контрактов (%s) — входы отклонятся.", exc)
        return {}


def print_contract_metadata(contracts) -> None:
    for ticker, meta in sorted(contracts.items()):
        log.info(
            "Контракт %s: шаг %.6f, стоимость шага %.4f, ГО(купли) %.2f, ГО(продажи) %.2f",
            ticker, meta.price_step, meta.step_cost, meta.go_buy, meta.go_sell,
        )


def _strategy_map():
    from src.strategies.macd_rsi_stoch_strategy import DEFAULT_CONFIG as MACD
    from src.strategies.flat_triangle_strategy import DEFAULT_CONFIG as FLAT
    from src.strategies.harmonic_abcd_strategy import DEFAULT_CONFIG as HARMONIC
    from src.strategies.ma_cloud_rsi_macd_strategy import DEFAULT_CONFIG as MA_CLOUD

    return {
        "macd_rsi_stoch": MACD,
        "flat_triangle": FLAT,
        "harmonic_abcd": HARMONIC,
        "ma_cloud_rsi_macd": MA_CLOUD,
    }


if __name__ == "__main__":
    main()
