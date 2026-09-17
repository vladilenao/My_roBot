from src.market_context import (
    MarketContextCache,
    SRLevelsCalculator,
    TrendAnalyzer,
)

from src import __version__
from decimal import Decimal

from src.decision import SignalFilter
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
    DATABASE_FILE,
    JOURNAL_FILE,
    POSITIONS_FILE,
    RISK_LIMITS,
    AUDIT_FILE,
    AUDIT_MAX_BYTES,
    AUDIT_BACKUP_COUNT,
    trading_enabled,
    app_dir,
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
        timeframes=sorted(set(ACTIVE_TIMEFRAMES) | {"1m"}), sleep_secs=SLEEP_SECONDS
    )
    data_cache = MarketDataCache(
        loader=load_candles, timeline=timeline, token=TINKOFF_TOKEN
    )

    htf_provider = HtfFrameProvider(cache=data_cache, timeline=timeline)
    PROFILES["triple_screen"] = TripleScreenFilter(
        provider=htf_provider, params=TRIPLE_SCREEN_PARAMS
    )

    runtime = _build_runtime(instruments, notifier, data_cache)

    TradingBot(
        instruments=instruments,
        notifier=notifier,
        strategy_map=_strategy_map(),
        data_cache=data_cache,
        timeline=timeline,
        execution=runtime.execution,
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
        risk_manager=runtime.risk_manager,
        post_tick=runtime.post_tick,
        trade_manager=runtime.trade_manager,
    ).run()


class _Runtime:
    def __init__(self, execution, post_tick=None, trade_manager=None, risk_manager=None) -> None:
        self.execution = execution
        self.post_tick = post_tick
        self.trade_manager = trade_manager
        self.risk_manager = risk_manager


def _build_runtime(instruments, notifier, data_cache) -> _Runtime:
    """Compose notification-only or SQLite-backed simulated runtime services.

    Neither mode constructs an exchange adapter. The only broker selected here is
    the addressed candle simulator, and durable trade state is restored from
    SQLite rather than either legacy CSV projection.
    """
    if not trading_enabled():
        log.info("Торговый режим выключен — NotifyOnly.")
        return _Runtime(NotifyOnlyExecutionPort(notifier))

    from src.broker import create_addressable_journal_broker
    from src.portfolio import PortfolioRiskManager
    from src.trade_journal.storage import Storage
    from src.trade_management.manager import TradeManager

    try:
        storage = Storage(
            app_dir() / DATABASE_FILE,
            journal_path=app_dir() / JOURNAL_FILE,
            positions_path=app_dir() / POSITIONS_FILE,
            audit_path=app_dir() / AUDIT_FILE,
            audit_max_bytes=AUDIT_MAX_BYTES,
            audit_backup_count=AUDIT_BACKUP_COUNT,
        )
        broker = create_addressable_journal_broker(
            INITIAL_DEPOSIT,
            CLEARING_TIMES,
            contract_names=_instrument_names(instruments),
        )
    except Exception as exc:
        log.warning("Не удалось поднять SQLite-симуляцию (%s) — NotifyOnly.", exc)
        return _Runtime(NotifyOnlyExecutionPort(notifier))

    trade_manager = TradeManager(
        storage,
        broker,
        initial_balance=Decimal(str(INITIAL_DEPOSIT)),
    )
    trade_manager.restore()
    risk_manager = PortfolioRiskManager()
    # Validate the configured limits while building the runtime. Their pure
    # calculations are consumed by the admission layer as it creates plans.
    _risk_limits()
    contracts = _load_contracts_metadata(instruments)
    broker.set_contracts(contracts)
    broker.set_names(_instrument_names(instruments))
    print_contract_metadata(contracts)

    def on_bar(ready_tfs: set[str]) -> None:
        try:
            prices = {}
            bar_times = []
            for instrument in instruments:
                try:
                    frame = data_cache.frame_for(instrument, "1m")
                except Exception:
                    continue
                if frame.empty:
                    continue
                bar_time = frame["datetime"].iloc[-1]
                if hasattr(bar_time, "to_pydatetime"):
                    bar_time = bar_time.to_pydatetime()
                bar_times.append(bar_time)
                prices[instrument.ticker] = (
                    float(frame["open"].iloc[-1]),
                    float(frame["low"].iloc[-1]),
                    float(frame["high"].iloc[-1]),
                    float(frame["close"].iloc[-1]),
                )
            if prices:
                broker.track_bar(max(bar_times), prices, contracts)
            for event in broker.drain_events():
                notifier.notify_event(event.type, event.position_id, event.message)
        except Exception as exc:
            log.warning("Сбой обработки бара исполнением: %s", exc)

    return _Runtime(
        NotifyOnlyExecutionPort(notifier),
        post_tick=on_bar,
        trade_manager=trade_manager,
        risk_manager=risk_manager,
    )


def _risk_limits():
    """Build typed portfolio limits from the validated configuration values."""
    from src.portfolio import RiskLimits

    return RiskLimits(
        per_trade=Decimal(str(RISK_LIMITS["trade_pct"])),
        per_instrument=Decimal(str(RISK_LIMITS["instrument_pct"])),
        per_group={key: Decimal(str(value)) for key, value in RISK_LIMITS.get("groups", {}).items()},
        portfolio=Decimal(str(RISK_LIMITS["portfolio_pct"])),
    )


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
