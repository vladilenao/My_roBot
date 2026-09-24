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
    CATCH_UP_BARS,
    DATA_BACKFILL_WINDOW_SECONDS,
    DATA_REFRESH_MIN_INTERVAL,
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
    TRADE_MANAGEMENT_PROFILES,
    CONTRACT_EXPIRY_BLOCK_DAYS,
    trading_enabled,
    runtime_dir,
)
from src.data.cache import MarketDataCache
from src.data.htf_provider import HtfFrameProvider
from src.data.loader import load_candles
from src.decision.filters import PROFILES
from src.decision.filters.triple_screen import TripleScreenFilter
from src.execution import NotifyOnlyExecutionPort
from src.instruments import Instrument, normalize_instrument
from src.instruments.selector import select_instruments
from src.logging_setup import get_logger, setup_logging
from src.notifier import get_notifier
from src.scheduler.timing import MultiTimeframeScheduler

log = get_logger(__name__)


def main():
    state_dir = runtime_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(
        service_uid=LOGGING_SERVICE_UID,
        log_file=str(state_dir / LOGGING_FILE),
        level=LOGGING_LEVEL,
        max_bytes=LOGGING_MAX_BYTES,
        backup_count=LOGGING_BACKUP_COUNT,
    )
    log.info("Робот v%s запущен", __version__)
    instruments = select_instruments(validation_pause_secs=DATA_REFRESH_MIN_INTERVAL) or [(TICKER, TICKER, INSTRUMENT_TYPE)]
    notifier = get_notifier()
    timeline = MultiTimeframeScheduler(
        timeframes=sorted(set(ACTIVE_TIMEFRAMES) | {"1m"}), sleep_secs=SLEEP_SECONDS,
        catch_up_bars=CATCH_UP_BARS,
    )
    data_cache = MarketDataCache(
        loader=load_candles,
        timeline=timeline,
        token=TINKOFF_TOKEN,
        data_refresh_min_interval=DATA_REFRESH_MIN_INTERVAL,
        data_backfill_window_seconds=DATA_BACKFILL_WINDOW_SECONDS,
        freshness_tolerance_bars=CATCH_UP_BARS,
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
        action_executor=runtime.action_executor,
    ).run()


class _Runtime:
    def __init__(self, execution, post_tick=None, trade_manager=None, risk_manager=None,
                 action_executor=None) -> None:
        self.execution = execution
        self.post_tick = post_tick
        self.trade_manager = trade_manager
        self.risk_manager = risk_manager
        self.action_executor = action_executor


def _build_runtime(instruments, notifier, data_cache) -> _Runtime:
    """Compose notification-only or SQLite-backed simulated runtime services.

    Neither mode constructs an exchange adapter. The only broker selected here is
    the addressed candle simulator, and durable trade state is restored from
    SQLite rather than either legacy CSV projection.
    """
    instruments = [
        i if isinstance(i, Instrument) else normalize_instrument(i) for i in instruments
    ]
    if not trading_enabled():
        log.info("Торговый режим выключен — NotifyOnly.")
        return _Runtime(NotifyOnlyExecutionPort(notifier))

    from src.broker import create_addressable_journal_broker
    from src.portfolio import PortfolioRiskManager
    from src.trade_journal.storage import Storage
    from src.trade_management.manager import TradeManager

    try:
        state_dir = runtime_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        storage = Storage(
            state_dir / DATABASE_FILE,
            journal_path=state_dir / JOURNAL_FILE,
            positions_path=state_dir / POSITIONS_FILE,
            audit_path=state_dir / AUDIT_FILE,
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

    risk_limits = _risk_limits()
    trade_manager = TradeManager(
        storage,
        broker,
        initial_balance=Decimal(str(INITIAL_DEPOSIT)),
        profiles_config=TRADE_MANAGEMENT_PROFILES,
        risk_limits=risk_limits,
        max_qty=RISK_LIMITS.get("max_qty"),
        commission=RISK_LIMITS.get("commission"),
        slippage=RISK_LIMITS.get("slippage"),
        contract_expiry_block_days=CONTRACT_EXPIRY_BLOCK_DAYS,
        signal_filter=SignalFilter(),
    )
    trade_manager.restore()
    action_executor = _OutboxExecutor(trade_manager)
    risk_manager = PortfolioRiskManager()
    contracts = _load_contracts_metadata(instruments)
    broker.set_contracts(contracts)
    storage.set_contract_metadata(contracts)
    storage.set_names(_instrument_names(instruments))
    broker.set_names(_instrument_names(instruments))
    print_contract_metadata(contracts)

    def on_bar(ready_tfs: set[str]) -> None:
        try:
            prices = {}
            bar_times = []
            for instrument in instruments:
                try:
                    frame = data_cache.frame_for(instrument, "1m")
                except Exception as exc:
                    log.warning(
                        "Сбой загрузки бара 1m по %s: %s",
                        _instrument_ticker(instrument), exc,
                    )
                    continue
                if frame.empty:
                    continue
                bar_time = frame["datetime"].iloc[-1]
                if hasattr(bar_time, "to_pydatetime"):
                    bar_time = bar_time.to_pydatetime()
                try:
                    bar_times.append(bar_time)
                    prices[instrument.ticker] = (
                        float(frame["open"].iloc[-1]),
                        float(frame["low"].iloc[-1]),
                        float(frame["high"].iloc[-1]),
                        float(frame["close"].iloc[-1]),
                    )
                except Exception as exc:
                    log.warning(
                        "Сбой обработки бара %s по %s: %s",
                        bar_time, _instrument_ticker(instrument), exc,
                    )
                    continue
            if prices:
                broker.track_bar(max(bar_times), prices, contracts)
            for event in broker.drain_events():
                notifier.notify_event(event.type, event.position_id, event.message)
            for event in broker.drain_addressed_events():
                try:
                    trade_manager.consume(event)
                except Exception as exc:
                    log.warning(
                        "Сбой применения бара исполнением (%s): %s", event.execution_id, exc
                    )
        except Exception as exc:
            log.warning("Сбой обработки бара исполнением: %s", exc)

    return _Runtime(
        NotifyOnlyExecutionPort(notifier),
        post_tick=on_bar,
        trade_manager=trade_manager,
        risk_manager=risk_manager,
        action_executor=action_executor,
    )


class _OutboxExecutor:
    """Flushes the durable trade-management outbox after fresh intent is queued."""

    def __init__(self, trade_manager) -> None:
        self._trade_manager = trade_manager

    def submit(self, action, now) -> None:
        dispatch = getattr(self._trade_manager, "dispatch", None)
        if dispatch is None:
            return
        try:
            dispatch(now)
        except Exception as exc:
            log.warning("Сбой доставки команд исполнению: %s", exc)


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
