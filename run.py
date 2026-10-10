from src.market_context import (
    MarketContextCache,
    SRLevelsCalculator,
    TrendAnalyzer,
)

from src import __version__
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from shutil import rmtree
import sys

from src.decision import SignalFilter
from src.bot import TradingBot
from src.config import (
    ACTIVE_TIMEFRAMES,
    CATCH_UP_BARS,
    DATA_BACKFILL_WINDOW_SECONDS,
    DATA_REFRESH_MIN_INTERVAL,
    MARKET_DATA_DATABASE_FILE,
    MARKET_DATA_EXPORT_ENABLED,
    MARKET_DATA_EXPORT_HOST,
    MARKET_DATA_EXPORT_PORT,
    MARKET_DATA_EXPORT_TOKEN,
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
    TRADING_DIRECTIONS,
    trading_enabled,
    runtime_dir,
    run_dir_name,
    HIST_DIR,
)
from src.api.emulator_client import EmulatorClientProvider, EmulatorError
from src.bot.run_session import build_run_session
from src.data.timeutil import to_aware_utc, to_naive
from src.history.preflight import active_pairs, run_preflight, smallest_period
from src.history.report import (
    MarketDataSync,
    REPORT_TXT,
    RunMetrics,
    collect_result,
    completion_line,
    crash_line,
    write_report,
)
from src.scheduler.clock import HistoricalClock
from src.data.cache import MarketDataCache
from src.data.htf_provider import HtfFrameProvider
from src.data.loader import load_candles
from src.data.market_store import MarketDataStore
from src.data.export_api import MarketDataExportServer
from src.data.reconciliation import MarketDataReconciler
from src.data.poller import LiveMarketDataPoller
from src.decision.filters import PROFILES
from src.decision.filters.triple_screen import TripleScreenFilter
from src.events.bus import EventBus
from src.events.event import Event
from src.events.types import EventType
from src.instruments import Instrument, normalize_instrument
from src.instruments.selector import select_instruments
from src.logging_setup import get_logger, setup_logging
from src.notifier import build_channels, close_channels
from src.runtime_lock import RuntimeLockError, acquire_runtime_lock
from src.scheduler.timing import MultiTimeframeScheduler

log = get_logger(__name__)

MODE_LIVE = "live"
MODE_HISTORY = "history"
HISTORY_DEFAULT_PAUSE = 1.0
MOMENT_FORMAT = "%Y-%m-%d %H:%M"


class RunConfigurationError(Exception):
    """Прогон не запускается: ответы оператора не задают рабочий диапазон."""


def ask_mode(no_prompt: bool = False) -> str:
    """Режим прогона. ``--no-prompt`` — боевая торговля без единого вопроса."""
    if no_prompt:
        return MODE_LIVE
    print("=== Режим работы ===")
    print("  1. Боевая торговля (реальный брокер, текущее время)")
    print("  2. Историческая торговля (локальный эмулятор данных, заданный диапазон)")
    while True:
        answer = input("Выбор режима (1/2): ").strip()
        if answer == "1":
            return MODE_LIVE
        if answer == "2":
            return MODE_HISTORY
        print("Нужен ответ 1 или 2.")


def ask_history_range() -> tuple[datetime, datetime, float]:
    """Границы исторического прогона и пауза между тиками в секундах."""
    start = _ask_moment("Начало диапазона")
    end = _ask_moment("Конец диапазона (не входит в прогон)")
    pause = _ask_pause()
    return validate_range(start, end, pause)


def _ask_moment(prompt: str) -> datetime:
    while True:
        raw = input(f"{prompt} (ГГГГ-ММ-ДД ЧЧ:ММ): ").strip()
        try:
            return parse_moment(raw)
        except ValueError as exc:
            print(str(exc))


def _ask_pause() -> float:
    while True:
        raw = input(f"Пауза между тиками, с (Enter — {HISTORY_DEFAULT_PAUSE:g}): ").strip()
        if not raw:
            return HISTORY_DEFAULT_PAUSE
        try:
            value = float(raw)
        except ValueError:
            print("Пауза должна быть числом секунд.")
            continue
        if value < 0:
            print("Пауза не может быть отрицательной.")
            continue
        return value


def parse_moment(raw: str) -> datetime:
    """Момент из строки оператора: без зоны — UTC, с зоной — приводим к UTC."""
    text = raw.strip()
    if not text:
        raise ValueError("Пустая дата — введите момент в формате ГГГГ-ММ-ДД ЧЧ:ММ.")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"Не разобрана дата «{text}» — нужен формат ГГГГ-ММ-ДД ЧЧ:ММ."
        ) from None
    return to_naive(to_aware_utc(moment))


def validate_range(start: datetime, end: datetime, pause: float) -> tuple[datetime, datetime, float]:
    if to_naive(start) >= to_naive(end):
        raise RunConfigurationError(
            f"Начало диапазона ({start:%Y-%m-%d %H:%M}) должно быть раньше конца "
            f"({end:%Y-%m-%d %H:%M}). Прогон не запущен."
        )
    return to_naive(start), to_naive(end), float(pause)


def describe_scale(start: datetime, end: datetime, pause: float, timeframes=None) -> str:
    """Оценка объёма прогона: тики, реальное время и каталог состояния."""
    step = smallest_period(timeframes or (sorted(set(ACTIVE_TIMEFRAMES) | {"1m"})))
    ticks = int((to_naive(end) - to_naive(start)).total_seconds() // step.total_seconds())
    if pause:
        waiting = f"Оценка реального времени при паузе {pause:g} с: {_duration(timedelta(seconds=ticks * pause))}"
    else:
        waiting = "Ожидание между тиками: нет (прогон без пауз, длительность определяется обработкой)"
    return (
        f"=== Масштаб прогона ===\n"
        f"Шаг тика:        {step}\n"
        f"Обработано тиков: {ticks}\n"
        f"{waiting}\n"
        f"Каталог состояния: {state_dir_for(start, end)}"
    )


def _duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def state_dir_for(start: datetime, end: datetime, started_at: datetime | None = None) -> Path:
    """Каталог состояния исторического прогона — отдельный от боевого."""
    return runtime_dir(Path(HIST_DIR)) / run_dir_name(
        to_naive(start), to_naive(end), to_naive(started_at or datetime.now())
    )


def main(no_prompt: bool = False):
    try:
        mode = ask_mode(no_prompt)
        if mode == MODE_HISTORY:
            start, end, pause = ask_history_range()
            print(describe_scale(start, end, pause))
        else:
            start = end = pause = None
    except (RunConfigurationError, ValueError) as exc:
        print(str(exc))
        return 1

    if mode == MODE_HISTORY:
        return _run_history(start, end, pause)
    return _run_live()


def _run_live():
    try:
        code, _, _ = _launch(
            state_dir=runtime_dir(),
            client_provider=None,
            clock=None,
            session=build_run_session(MODE_LIVE),
            channel_names=None,
        )
    except RuntimeLockError as exc:
        print(str(exc))
        return 1
    return code


def _run_history(start, end, pause):
    """Прогон с повтором, если проверка сдвинула начало на первую доступную свечу.

    Сдвиг меняет часы и каталог состояния, поэтому прогон собирается заново; уже
    выбранные инструменты передаются в повтор, чтобы пользователя не спрашивали
    второй раз. Повтор ровно один — границы для нового начала проверены.
    """
    provider = EmulatorClientProvider()
    try:
        sync = provider.prepare_snapshot()
    except EmulatorError as exc:
        market_data_sync = MarketDataSync(reason=exc.message)
        print(f"Импорт My Robot перед прогоном не подтверждён: {exc.message}")
    else:
        if sync["synchronized"]:
            market_data_sync = MarketDataSync(
                status="synchronized",
                producer_id=sync["producer_id"],
                target_change_id=sync["target_change_id"],
                after_id=sync["after_id"],
                snapshot_generation=sync["snapshot_generation"],
            )
            print(
                "Импорт My Robot подтверждён: "
                f"снимок {sync['snapshot_generation']}, "
                f"курсор {sync['after_id']}/{sync['target_change_id']}."
            )
        else:
            source = sync.get("source")
            state = source.get("state") if isinstance(source, dict) else None
            reason = (
                f"эмулятор сообщил состояние {state}"
                if state in {"incomplete", "blocked", "error"}
                else "эмулятор не подтвердил полноту журнала"
            )
            market_data_sync = MarketDataSync(
                producer_id=sync["producer_id"],
                target_change_id=sync["target_change_id"],
                after_id=sync["after_id"],
                reason=reason,
            )
            print(f"Импорт My Robot перед прогоном не подтверждён: {reason}")
    instruments = None
    for _ in range(2):
        clock = HistoricalClock(start, end, smallest_period(_timeframes()), pause)
        state_dir = state_dir_for(start, end)
        code, shifted, instruments = _launch(
            state_dir=state_dir,
            client_provider=provider,
            clock=clock,
            session=build_run_session(MODE_HISTORY, clock),
            channel_names=[],
            history=True,
            instruments=instruments,
            market_data_sync=market_data_sync,
        )
        if shifted is None:
            return code
        rmtree(state_dir, ignore_errors=True)
        start = shifted
    return code


def _timeframes() -> list[str]:
    return sorted(set(ACTIVE_TIMEFRAMES) | {"1m"})


def _warmup_bars() -> int:
    """Сколько баров прогрева требуют активные стратегии — проверка границ."""
    from src.strategies.registry import get_strategy

    needed = 1
    for assignments in (SHARE_STRATEGIES, FUTURE_STRATEGIES):
        for group in assignments.values():
            for assignment in group:
                config = _strategy_map().get(assignment.strategy)
                if config is None:
                    continue
                try:
                    needed = max(
                        needed, get_strategy(assignment.strategy, config).required_history()
                    )
                except Exception as exc:  # noqa: BLE001 — стратегию проверит бот при старте
                    log.debug("Прогрев для %s не посчитан: %s", assignment.strategy, exc)
    return needed


def _source_ping(client_provider):
    """Проверка живости источника: у эмулятора есть ``/health``, у T-API нет."""
    client_context = getattr(client_provider, "client_context", None)
    if client_context is None:
        return None

    def ping() -> None:
        with client_context() as client:
            probe = getattr(client, "ping", None)
            if probe is not None:
                probe()

    return ping


def _launch(
    *,
    state_dir,
    client_provider,
    clock,
    session,
    channel_names,
    history: bool = False,
    instruments=None,
    market_data_sync: MarketDataSync | None = None,
) -> tuple[int, datetime | None, list]:
    """Единая сборка прогона: различаются только источник, часы и каталог.

    Всё остальное — кэш, планировщик, стратегии, исполнение и журнал — собирается
    одинаково, поэтому исторический прогон идёт по тому же конвейеру, что и боевой.

    Возвращает код завершения, сдвиг начала и выбранные инструменты: если
    проверка перенесла начало на первую доступную свечу, собранные часы и каталог
    уже не годятся, а прогонать диапазон должен вызывающий — он же передаст
    сюда ``instruments``, чтобы не спрашивать пользователя повторно.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    if not history:
        acquire_runtime_lock(state_dir)
    setup_logging(
        service_uid=LOGGING_SERVICE_UID,
        log_file=str(state_dir / LOGGING_FILE),
        level=LOGGING_LEVEL,
        max_bytes=LOGGING_MAX_BYTES,
        backup_count=LOGGING_BACKUP_COUNT,
    )
    log.info("Робот v%s запущен", __version__)
    if instruments is None:
        instruments = select_instruments(
            validation_pause_secs=0.0 if history else DATA_REFRESH_MIN_INTERVAL,
            client_provider=client_provider,
            market_now=None if clock is None else clock.now(),
        ) or [(TICKER, TICKER, INSTRUMENT_TYPE)]
        instruments = [normalize_instrument(item) for item in instruments]

    if history:
        report = run_preflight(
            active_pairs(instruments, SHARE_STRATEGIES, FUTURE_STRATEGIES),
            start=session.start,
            end=session.end,
            warmup_bars=_warmup_bars(),
            load_candles=load_candles,
            client_provider=client_provider,
            clock=clock,
            ping=_source_ping(client_provider),
        )
        if not report.ok:
            print(report.message())
            return 2, None, instruments
        if report.start != session.start:
            print(report.message())
            return 3, report.start, instruments
        if report.notes:
            print(report.message())

    channels = build_channels(channel_names, state_dir=state_dir)
    bus = EventBus()
    bus.subscribe_all(channels)
    timeline = MultiTimeframeScheduler(
        timeframes=_timeframes(),
        sleep_secs=SLEEP_SECONDS if clock is None else clock.pause_secs,
        catch_up_bars=CATCH_UP_BARS,
        clock=clock,
    )
    market_store = None if history else MarketDataStore(state_dir / MARKET_DATA_DATABASE_FILE)
    market_reconciler = None if market_store is None else MarketDataReconciler(
        market_store, source="tbank_exchange", token=TINKOFF_TOKEN,
        client_provider=client_provider,
    )
    data_cache = MarketDataCache(
        loader=load_candles,
        timeline=timeline,
        token=TINKOFF_TOKEN,
        data_refresh_min_interval=DATA_REFRESH_MIN_INTERVAL if clock is None else 0.0,
        data_backfill_window_seconds=None if clock is not None else DATA_BACKFILL_WINDOW_SECONDS,
        freshness_tolerance_bars=CATCH_UP_BARS,
        clock=clock,
        client_provider=client_provider,
        market_store=market_store,
        market_reconciler=market_reconciler,
    )

    htf_provider = HtfFrameProvider(cache=data_cache, timeline=timeline)
    PROFILES["triple_screen"] = TripleScreenFilter(
        provider=htf_provider, params=TRIPLE_SCREEN_PARAMS
    )

    storage = None
    export_server = None
    market_poller = None
    try:
        if not history:
            market_poller = LiveMarketDataPoller(data_cache, _timeframes(), TICK_POLL_SECS)
            market_poller.start()
        if not history and MARKET_DATA_EXPORT_ENABLED:
            export_server = MarketDataExportServer(
                market_store, MARKET_DATA_EXPORT_TOKEN or "",
                host=MARKET_DATA_EXPORT_HOST, port=MARKET_DATA_EXPORT_PORT,
            )
            export_server.start()
        runtime = _build_runtime(
            instruments, data_cache, bus, state_dir, clock=clock, session=session,
            client_provider=client_provider,
        )
        storage = runtime.storage
        _run_bot(instruments, bus, runtime, data_cache, timeline, session=session)
    finally:
        if market_poller is not None:
            market_poller.close()
        if export_server is not None:
            export_server.close()
        if history:
            _finish_history(
                storage, session, data_cache, timeline, state_dir, market_data_sync
            )
        close_channels(channels)
    return 0, None, instruments


def _finish_history(
    storage, session, data_cache, timeline, state_dir,
    market_data_sync: MarketDataSync | None = None,
) -> None:
    """Отчёт и одна строка консоли — и для штатного, и для аварийного финиша."""
    if storage is None:
        return
    reason = session.stop_reason()
    crashed = not reason
    longest_gap = getattr(data_cache, "longest_gap", None)
    exhaustion = getattr(session, "exhaustion", None)
    metrics = RunMetrics(
        start=session.start,
        end=session.end,
        ticks=session.ticks_done(),
        missed_bars=data_cache.missed_bars,
        gaps=getattr(data_cache, "gaps", 0),
        longest_gap_seconds=0.0 if longest_gap is None else longest_gap.total_seconds(),
        covered=bool(getattr(session, "covered", False)),
        stop_reason=reason or "остановлено оператором",
        market_now=to_naive(timeline.now()),
        crashed=crashed,
        horizon_start=None if exhaustion is None else exhaustion.horizon_start,
        horizon_end=None if exhaustion is None else exhaustion.horizon_end,
        horizon_limited=bool(getattr(exhaustion, "horizon_limited", False)),
        market_data_sync=market_data_sync or MarketDataSync(),
    )
    write_report(
        state_dir, metrics, collect_result(storage), source=str(state_dir / DATABASE_FILE)
    )
    print(crash_line(metrics) if crashed else completion_line(state_dir / REPORT_TXT))



def _run_bot(instruments, bus, runtime, data_cache, timeline, session=None) -> None:
    TradingBot(
        instruments=instruments,
        bus=bus,
        strategy_map=_strategy_map(),
        data_cache=data_cache,
        timeline=timeline,
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
        post_tick=runtime.post_tick,
        trade_manager=runtime.trade_manager,
        action_executor=runtime.action_executor,
        run=session,
    ).run()


class _Runtime:
    def __init__(self, post_tick=None, trade_manager=None, action_executor=None, storage=None) -> None:
        self.post_tick = post_tick
        self.trade_manager = trade_manager
        self.action_executor = action_executor
        self.storage = storage


def _build_runtime(
    instruments, data_cache, bus, state_dir, clock=None, session=None, client_provider=None
) -> _Runtime:
    """Compose notification-only or SQLite-backed simulated runtime services.

    Neither mode constructs an exchange adapter. The only broker selected here is
    the addressed candle simulator, and durable trade state is restored from
    SQLite rather than either legacy CSV projection. ``state_dir`` — каталог
    состояния прогона: общий для боевого режима, отдельный у исторического.
    """
    instruments = [
        i if isinstance(i, Instrument) else normalize_instrument(i) for i in instruments
    ]
    if not trading_enabled():
        log.info("Торговый режим выключен — NotifyOnly.")
        return _Runtime()

    from src.broker import create_addressable_journal_broker
    from src.trade_journal.storage import Storage
    from src.trade_management.manager import TradeManager

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        storage = Storage(
            state_dir / DATABASE_FILE,
            journal_path=state_dir / JOURNAL_FILE,
            positions_path=state_dir / POSITIONS_FILE,
            audit_path=state_dir / AUDIT_FILE,
            audit_max_bytes=AUDIT_MAX_BYTES,
            audit_backup_count=AUDIT_BACKUP_COUNT,
            initial_deposit=str(INITIAL_DEPOSIT),
            clock=clock,
        )
        broker = create_addressable_journal_broker(
            INITIAL_DEPOSIT,
            CLEARING_TIMES,
            contract_names=_instrument_names(instruments),
        )
    except Exception as exc:
        log.warning("Не удалось поднять SQLite-симуляцию (%s) — NotifyOnly.", exc)
        return _Runtime()

    risk_limits = _risk_limits()
    names = _instrument_names(instruments)

    def publish_execution(execution, details):
        action = details["action_type"].split(":", 1)[0]
        if str(execution.status) == "reject":
            event_type = EventType.ORDER_REJECTED
        elif str(execution.status) == "cancel":
            event_type = EventType.TRADE_CANCELLED
        elif action == "MOVESTOP":
            event_type = EventType.STOP_MOVED
        else:
            event_type = {"OPEN": EventType.TRADE_OPENED, "ADD": EventType.POSITION_ADDED,
                      "STOP": EventType.STOP_HIT, "TARGET": EventType.TARGET_HIT,
                      "REDUCE": EventType.TRADE_CLOSED, "CLOSE": EventType.TRADE_CLOSED}[action]
        bus.publish(Event.broker_event(event_type, trade_id=execution.trade_id,
            instrument=names.get(details["instrument_id"]) or "контракт не указан",
            **({"side": details["side"]} if details.get("side") else {}),
            bar_time=execution.timestamp, quantity=execution.filled_quantity, price=execution.price,
             reason=execution.reason,
             **({"fee": execution.fee, "fee_source": str(execution.fee_source)}
                if str(execution.status) in {"fill", "partial"} else {}),
             execution_id=execution.execution_id, status=str(execution.status),
             visual=details.get("visual"),
             timeframe=details["visual"].data["timeframe"] if details.get("visual") else "",
             **{key: details[key] for key in ("gross_pnl", "net_pnl", "fees_total", "fees_known", "pnl_units", "quantity_remaining",
                                            "requested_quantity", "selected_quantity", "limiting_constraint")
                if key in details and str(execution.status) in {"fill", "partial"}}))

    trade_manager = TradeManager(
        storage,
        broker,
        initial_balance=Decimal(str(INITIAL_DEPOSIT)),
        budget_observer=lambda details: bus.publish(Event.broker_event(EventType.RISK_LIMIT_HIT, risk_scope="portfolio", **details)),
        execution_observer=publish_execution,
        profiles_config=TRADE_MANAGEMENT_PROFILES,
        risk_limits=risk_limits,
        max_qty=RISK_LIMITS.get("max_qty"),
        commission=RISK_LIMITS.get("commission"),
        slippage=RISK_LIMITS.get("slippage"),
        slippage_tolerance=RISK_LIMITS.get("slippage_tolerance"),
        min_trade_risk_pct=RISK_LIMITS.get("min_trade_risk_pct"),
        portfolio_pct=RISK_LIMITS.get("portfolio_pct", 2),
        min_risk_cost_ratio=RISK_LIMITS.get("min_risk_cost_ratio", 2),
        min_net_payoff=RISK_LIMITS.get("min_net_payoff", 1.5),
        max_slippage_r=RISK_LIMITS.get("max_slippage_r", 0.25),
        contract_expiry_block_days=CONTRACT_EXPIRY_BLOCK_DAYS,
        direction_limits=TRADING_DIRECTIONS,
        signal_filter=SignalFilter(),
        clock=clock,
    )
    trade_manager.restore()
    trade_manager.configure_visual_context(instruments)
    action_executor = _OutboxExecutor(trade_manager)
    contracts = _load_contracts_metadata(instruments, client_provider=client_provider)
    broker.set_contracts(contracts)
    storage.set_contract_metadata(contracts)
    storage.set_names(_instrument_names(instruments))
    broker.set_names(_instrument_names(instruments))
    print_contract_metadata(contracts)

    # Cursors advance only after executions and observations of a minute have
    # committed. On restart SQLite provides the safe boundary of live state.
    execution_cursors = storage.execution_bar_boundaries()
    execution_closes = {}
    pending_bar = None

    def commit_bar(stamp, prices, addressed, broker_events):
        instrument_bar_times = {ticker: stamp for ticker in prices}
        for event in addressed:
            trade_manager.consume(event)
        addressed_ids = {event.trade_id for event in addressed}
        for event in broker_events:
            if event.trade_id not in addressed_ids:
                bus.publish(_broker_event(event))
        trade_manager.observe_bars(prices, instrument_bar_times)
        execution_closes.update({ticker: values[3] for ticker, values in prices.items()})
        trade_manager.mark_to_market(execution_closes)
        # Только после всех durable операций минуты курсоры обеих БД могут
        # продвинуться. При исключении pending_bar будет повторён.
        for ticker in prices:
            execution_cursors[ticker] = (stamp, False)
            instrument = next((item for item in instruments if item.ticker == ticker), None)
            mark_processed = getattr(data_cache, "mark_processed", None)
            if instrument is not None and callable(mark_processed):
                mark_processed(instrument, "1m", stamp)

    def on_bar(ready_tfs: set[str]) -> None:
        nonlocal pending_bar
        try:
            # The simulator has already advanced if a durable write failed.
            # Retry those same events before asking it to execute another bar.
            if pending_bar is not None:
                commit_bar(*pending_bar)
                pending_bar = None
            batches = {}
            initial_boundaries = None
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
                try:
                    ticker = instrument.ticker
                    stamps = frame["datetime"].map(to_aware_utc)
                    boundary = execution_cursors.get(ticker)
                    if boundary is None:
                        if initial_boundaries is None:
                            initial_boundaries = storage.execution_bar_boundaries()
                        boundary = initial_boundaries.get(ticker, (stamps.max(), True))
                    last, inclusive = boundary
                    fresh = frame.loc[stamps >= last if inclusive else stamps > last]
                    fresh = fresh.sort_values("datetime").drop_duplicates("datetime", keep="last")
                    contiguous = getattr(data_cache, "contiguous_after", None)
                    if callable(contiguous):
                        protected = contiguous(instrument, "1m", fresh, last)
                        # Старые адаптеры и тестовые double не обязаны знать
                        # новый контракт покрытия; в таком случае оставляем
                        # исходный DataFrame.
                        if isinstance(protected, type(fresh)):
                            fresh = protected
                    for row in fresh.itertuples(index=False):
                        stamp = to_aware_utc(row.datetime).to_pydatetime()
                        batches.setdefault(stamp, {})[ticker] = (
                            float(row.open), float(row.low), float(row.high), float(row.close),
                        )
                except Exception as exc:
                    log.warning(
                        "Сбой подготовки минутных баров по %s: %s",
                        _instrument_ticker(instrument), exc,
                    )
                    continue
            for stamp, prices in sorted(batches.items()):
                instrument_bar_times = {ticker: stamp for ticker in prices}
                broker.track_bar(stamp, prices, contracts, bar_times=instrument_bar_times)
                pending_bar = (stamp, prices, list(broker.drain_addressed_events()), list(broker.drain_events()))
                commit_bar(*pending_bar)
                pending_bar = None
        except Exception as exc:
            log.warning("Сбой обработки бара исполнением: %s", exc)

    return _Runtime(
        post_tick=on_bar,
        trade_manager=trade_manager,
        action_executor=action_executor,
        storage=storage,
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


def _broker_event(event) -> Event:
    """Publish one structured executor fact without a single word of wording.

    Clearing is a portfolio-wide snapshot rather than a trade outcome, so it
    carries neither a trade nor a contract name and has its own constructor.
    """
    if event.type is EventType.CLEARING_DONE:
        return Event.clearing_done(
            balance=event.payload.get("balance"),
            positions=event.payload.get("positions", 0),
            bar_time=event.ts,
        )
    return Event.broker_event(
        event.type,
        trade_id=event.trade_id,
        instrument=event.instrument,
        bar_time=event.ts,
        **event.payload,
    )


def _risk_limits():
    """Build typed portfolio limits from the validated configuration values."""
    from src.portfolio import RiskLimits

    return RiskLimits(
        per_trade=Decimal(str(RISK_LIMITS.get("portfolio_pct", 2))),
        per_instrument=Decimal(str(RISK_LIMITS.get("portfolio_pct", 2))),
        per_group={},
        portfolio=Decimal(str(RISK_LIMITS.get("portfolio_pct", 2))),
    )


def _instrument_ticker(instrument) -> str | None:
    """Реальный тикер инструмента: для селектора — второй элемент кортежа (display, ticker, ...)."""
    if isinstance(instrument, (tuple, list)):
        return str(instrument[1]) if len(instrument) > 1 else str(instrument[0])
    return getattr(instrument, "ticker", None)


def _instrument_names(instruments) -> dict[str, str]:
    """Карта тикер -> короткое имя (NG-10.26) из селектора/нормализации инструментов.

    В карту попадают только настоящие короткие имена: тикер и полный label
    пользователю не показываются, для неизвестного контракта остаётся
    ``контракт не указан``.
    """
    names: dict[str, str] = {}
    for instrument in instruments:
        ticker = _instrument_ticker(instrument)
        if not ticker:
            continue
        if isinstance(instrument, (tuple, list)):
            short = instrument[3] if len(instrument) > 3 else None
        else:
            short = getattr(instrument, "short_name", None)
        if short:
            names[ticker] = short
    return names


def _load_contracts_metadata(instruments, client_provider=None):
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
    with context as client:
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


_UTF8_STREAMS_SET = False


def _force_utf8_streams() -> None:
    """Запуск без терминала (pipe, CI) на Windows использует cp1252.

    Замороженный PyInstaller-бинарь не подхватывает PYTHONUTF8 из окружения и
    падает на кириллице при перенаправлении stdout. Локальная консоль не
    трогается: там Python сам выбирает подходящий codepage.
    """
    global _UTF8_STREAMS_SET
    if _UTF8_STREAMS_SET:
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None or getattr(stream, "isatty", lambda: False)():
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    _UTF8_STREAMS_SET = True


def _config_smoke() -> None:
    """Проверяет bundled default.toml без пользовательских файлов и сети."""
    from src.config import _DEFAULTS
    from src.config_loader import load_config

    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    load_config(_DEFAULTS, config_file=root / "missing-robot.toml", bundled_file=root / "default.toml")
    print("Конфигурация по умолчанию загружена.")


def _run_smoke_command(args: list[str]) -> bool:
    """Выполняет неинтерактивные проверки binary и сообщает, была ли команда."""
    if "--version" in args:
        print(__version__)
        return True
    if "--config-smoke" in args:
        _config_smoke()
        return True
    if "--telegram-chart-smoke" in args:
        from src.notifier.telegram_chart import smoke

        smoke()
        return True
    return False


if __name__ == "__main__":
    _force_utf8_streams()
    if _run_smoke_command(sys.argv[1:]):
        pass
    elif any(flag in sys.argv[1:] for flag in ("--telegram-pending", "--telegram-retry", "--telegram-cleanup-files")):
        import argparse
        from src import config
        from src.notifier.telegram_recovery import cleanup, list_pending, retry

        parser = argparse.ArgumentParser(description="Восстановление уведомлений Telegram")
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--telegram-pending", action="store_true")
        action.add_argument("--telegram-retry", action="store_true")
        action.add_argument("--telegram-cleanup-files", action="store_true")
        parser.add_argument("--state-dir", type=Path, default=runtime_dir())
        parser.add_argument("--id", type=int, dest="operation_id")
        parser.add_argument("--trade-id")
        parser.add_argument("--from", dest="day_from")
        parser.add_argument("--to", dest="day_to")
        parser.add_argument("--include-uncertain", action="store_true")
        args = parser.parse_args()
        try:
            if args.telegram_pending:
                code = list_pending(config, args.state_dir, trade_id=args.trade_id, day_from=args.day_from, day_to=args.day_to)
            elif args.telegram_cleanup_files:
                code = cleanup(config, args.state_dir)
            else:
                # Ручной replay не должен пересекаться с работающим роботом.
                acquire_runtime_lock(args.state_dir)
                code = retry(config, args.state_dir, operation_id=args.operation_id, trade_id=args.trade_id,
                             day_from=args.day_from, day_to=args.day_to, include_uncertain=args.include_uncertain)
        except (RuntimeLockError, ValueError) as exc:
            print(str(exc))
            code = 2
        sys.exit(code)
    else:
        sys.exit(main(no_prompt="--no-prompt" in sys.argv[1:]) or 0)
