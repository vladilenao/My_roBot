"""Торговый контур: каналы, кластерный вердикт, торговая часть, оркестратор и отчёт истории."""

from src.market_context import (
    MarketContextCache,
    SRLevelsCalculator,
    TrendAnalyzer,
)

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import json

from src.decision import SignalFilter
from src.bot import TradingBot
from src.config import (
    MARKET_DATA_SETTINGS,
    FUTURE_STRATEGIES,
    HEARTBEAT_EVERY_TICKS,
    SHARE_STRATEGIES,
    TICK_POLL_SECS,
    TICK_TIMEOUT_SECS,
    TRIPLE_SCREEN_PARAMS,
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
    CONTRACT_EXPIRY_CLOSE_BEFORE_END_MIN,
    TRADING_DIRECTIONS,
    INTRADAY_SETTINGS,
    trading_enabled,
    TIMEFRAMES,
)
from src.data.timeutil import to_aware_utc, to_naive
from src.history.preflight import active_pairs
from src.history.selection import HistorySelection
from src.history.warmup import obligation_requirements, warmup_requirements
from src.history.contract_lifecycle import ConstantContractLifecycle, RolloverLifecycle
from src.history.report import (
    CalendarScheduleMetadata,
    MarketDataSync,
    REPORT_TXT,
    RunMetrics,
    collect_result,
    collect_rollover,
    completion_line,
    crash_line,
    write_report,
)
from src.scheduler.clock import HistoricalClock, as_aware
from src.data.htf_provider import HtfFrameProvider
from src.decision.filters import PROFILES
from src.decision.filters.triple_screen import TripleScreenFilter
from src.events.bus import EventBus
from src.events.event import Event
from src.events.types import EventType
from src.instruments import Instrument, normalize_instrument
from src.instruments.model import instrument_key
from src.logging_setup import get_logger, log_rotation
from src.notifier import build_channels, close_channels


from dataclasses import dataclass

from src.assembly.services import ContourDemand
# Общее с ядром — в нейтральном модуле; имена здесь, чтобы подмены в тестах
# и вызовы контура шли через этот модуль.
from src.assembly.contracts import (  # noqa: F401
    _instrument_ticker, _instrument_type, _load_contracts_metadata, _pattern_pairs, _token_client_context,
)

log = get_logger(__name__)


def _canonical_execution_boundaries(boundaries, aliases):
    """Map legacy ticker obligations to the UID used by live market pairs."""
    result = {}
    for identity, boundary in boundaries.items():
        key = aliases.get(identity, identity)
        previous = result.get(key)
        if previous is None or boundary[0] < previous[0]:
            result[key] = boundary
        elif boundary[0] == previous[0]:
            result[key] = (boundary[0], boundary[1] or previous[1])
    return result


def _market_obligations(boundaries, aliases, instruments, data_cache):
    """Обязательства журнала под UID рыночной пары (fix-live-pair-identity).

    Live-инструменты выбора не знают UID, поэтому их адрес — тикер, а полосы
    ищут обязательство по UID пары. Не найденная пара остаётся под прежним
    ключом: её переведёт следующий тик, когда пара зарегистрируется.
    """
    by_key = {instrument_key(instrument): instrument for instrument in instruments}
    result = {}
    for key, boundary in _canonical_execution_boundaries(boundaries, aliases).items():
        instrument = by_key.get(key)
        uid = (data_cache.pair_uid(instrument, "1m") if instrument is not None
               else _journal_pair_uid(data_cache, key))
        target = uid or key
        previous = result.get(target)
        if previous is None or boundary[0] < previous[0]:
            result[target] = boundary
        elif boundary[0] == previous[0]:
            result[target] = (boundary[0], boundary[1] or previous[1])
    return result


def _journal_pair_uid(data_cache, identity):
    """UID единственной 1m-пары, чей UID или тикер совпадает с адресом сделки в журнале."""
    found = {pair["uid"] for pair in data_cache.live_pairs()
             if pair["timeframe"] == "1m" and identity in (pair["uid"], pair["instrument"].ticker)}
    return found.pop() if len(found) == 1 else None


def _family_warmup(family):
    return warmup_requirements(family, FUTURE_STRATEGIES, _strategy_map(),
                              TRADE_MANAGEMENT_PROFILES, TRIPLE_SCREEN_PARAMS, tuple(TIMEFRAMES))


def _calendar_schedule(data_cache) -> CalendarScheduleMetadata | None:
    """Расписание, которым пользовался прогон; None — календаря у прогона нет."""
    calendar = getattr(data_cache, "market_calendar", None)
    usage = getattr(calendar, "usage", None)
    if not callable(usage):
        return None
    try:
        return CalendarScheduleMetadata(**usage())
    except Exception as exc:  # отчёт не должен падать из-за диагностики
        log.warning("Сводка расписания для отчёта не собрана: %s", exc)
        return None


def _strategy_admissions(session, storage) -> tuple:
    """Сигналы и недопуски по привязкам для отчёта; сбой диагностики отчёт не роняет."""
    stats = getattr(session, "admission_stats", None)
    if stats is None:
        return ()
    try:
        from src.history.admission_stats import trades_by_assignment

        return stats.rows(trades_by_assignment(storage.connection))
    except Exception as exc:
        log.warning("Недопуски по стратегиям для отчёта не собраны: %s", type(exc).__name__)
        return ()


def _finish_history(
    storage, session, data_cache, timeline, state_dir,
    market_data_sync: MarketDataSync | None = None,
) -> None:
    """Отчёт и одна строка консоли — и для штатного, и для аварийного финиша."""
    if storage is None:
        return
    original_reason = session.stop_reason()
    lifecycle = getattr(session, "contract_lifecycle", None)
    if isinstance(lifecycle, RolloverLifecycle):
        lifecycle.finish(timeline.now())
        if any(state["phase"] == "DRAINING" for state in lifecycle.states.values()):
            session.mark_contract_failure("старые обязательства не завершены к концу прогона")
    reason = session.stop_reason()
    crashed = not original_reason
    longest_gap = getattr(data_cache, "longest_gap", None)
    exhaustion = getattr(session, "exhaustion", None)
    last_processed = getattr(data_cache, "last_processed_bar", None)
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
        boundary_checks=tuple(getattr(session, "boundary_checks", ())),
        last_processed_bar=last_processed if isinstance(last_processed, datetime) else None,
        terminal_gap=bool(getattr(session, "terminal_gap", None)),
        rollover=collect_rollover(storage, lifecycle.selection) if isinstance(lifecycle, RolloverLifecycle) else None,
        calendar_schedule=_calendar_schedule(data_cache),
        log_rotation=log_rotation(),
        strategy_admissions=_strategy_admissions(session, storage),
    )
    write_report(
        state_dir, metrics, collect_result(storage), source=str(state_dir / DATABASE_FILE)
    )
    print(crash_line(metrics) if crashed else completion_line(state_dir / REPORT_TXT))


def _run_bot(instruments, bus, runtime, data_cache, timeline, session=None, live_waiter=None,
             update_obligations=None, after_tick=None, control=None, forget=None) -> None:
    def post_tick(ready_tfs):
        # Желаемое ядра применяется на границе тика, до сопровождения и исполнения (add-control-api).
        if control is not None:
            control.before_tick()
        # Обязательства после тика контур считает сам и передаёт службам.
        if update_obligations is not None and runtime.storage is not None:
            update_obligations(_market_obligations(
                runtime.storage.execution_bar_boundaries(), runtime.execution_aliases,
                runtime.execution_instruments, data_cache))
        result = runtime.post_tick(ready_tfs) if runtime.post_tick is not None else True
        if control is not None:
            control.after_tick()
        # Курсор ленты подтверждается только после commit тика.
        if after_tick is not None and result is not False:
            after_tick()
        return result

    bot = TradingBot(
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
        market_data_settings=MARKET_DATA_SETTINGS,
        live_data_waiter=live_waiter,
        context_cache=MarketContextCache(
            data_cache=data_cache,
            trend_analyzer=TrendAnalyzer(),
            sr_calculator=SRLevelsCalculator(),
        ),
        signal_filter=SignalFilter(),
        # Без финансовой части хук нужен только для команд ядра.
        post_tick=post_tick if runtime.post_tick is not None or control is not None else None,
        trade_manager=runtime.trade_manager,
        contract_lifecycle=runtime.contract_lifecycle,
        action_executor=runtime.action_executor,
        run=session,
    )
    if control is not None:
        control.start(runtime.trade_manager, _Roster(bot, runtime, data_cache, forget))
    try:
        bot.run()
    finally:
        if control is not None:
            control.close()


class _Roster:
    """Состав торговой части для ``ContourControl`` (add-contour-membership).

    Знает внутренности бота и живого анализа, чтобы модуль управления их не
    знал: прогрев, готовность пар, обзор и вход в цикл, выход из цикла.
    """

    def __init__(self, bot, runtime, cache, forget=None) -> None:
        self._bot = bot
        self._runtime = runtime
        self._cache = cache
        self._forget = forget

    def keys(self) -> set:
        return {instrument_key(item) for item in self._bot._instruments}

    def instrument(self, key):
        return next((item for item in self._bot._instruments if instrument_key(item) == key), None)

    def warm(self, instrument) -> bool:
        """Потребности прогрева — ядру; ``False``, пока живой анализ ещё не запущен."""
        live = self._bot._live_analysis
        if live is None:
            return False
        live.register_needs_for(instrument)
        return True

    def ready(self, instrument) -> bool:
        timeframes = {self._bot._protection_timeframe} | {a.timeframe for a in self._bot._strategies_for(instrument)}
        status = getattr(self._cache, "pair_status", None)
        return status is None or all(status(instrument, tf)["state"] != "BOOTSTRAPPING" for tf in timeframes)

    def admit(self, instrument, contracts=None) -> None:
        """Стартовый обзор без переигрывания истории, затем инструмент — в цикле анализа и исполнения."""
        self._runtime.add_instrument(instrument, contracts)
        live = self._bot._live_analysis
        if live is not None:
            live.bootstrap_instrument(instrument)
        if instrument_key(instrument) not in self.keys():
            self._bot._instruments.append(instrument)

    def retire(self, instrument) -> None:
        key = instrument_key(instrument)
        self._bot._instruments[:] = [item for item in self._bot._instruments if instrument_key(item) != key]
        self._runtime.remove_instrument(instrument)
        if self._bot._live_analysis is not None:
            self._bot._live_analysis.forget_instrument(instrument)
        if self._forget is not None:
            self._forget(instrument)


class _UnsafeExecutionState(RuntimeError):
    """The in-memory simulator must be rebuilt from durable state."""

    fatal_execution = True


class _Runtime:
    def __init__(self, post_tick=None, trade_manager=None, action_executor=None, storage=None, contract_lifecycle=None) -> None:
        self.post_tick = post_tick
        self.trade_manager = trade_manager
        self.action_executor = action_executor
        self.storage = storage
        self.contract_lifecycle = contract_lifecycle
        self.ingestion_worker = None
        self.execution_aliases = {}
        self.execution_instruments = []
        # Финансовая часть (add-contour-membership): список допуска доставки и приём карточек контрактов.
        self.instruments = None
        self.on_contracts = None

    def add_instrument(self, instrument, contracts=None) -> None:
        """Инструмент вошёл в состав на ходу: карточки, жизненный цикл, исполнение, алиасы."""
        key = instrument_key(instrument)
        for target in self._instrument_lists():
            if key not in {instrument_key(item) for item in target}:
                target.append(instrument)
        if self.storage is not None:
            self.execution_aliases.update({key: key, instrument.ticker: key})
        if contracts and self.on_contracts is not None:
            self.on_contracts(contracts)

    def remove_instrument(self, instrument) -> None:
        """Инструмент вышел из состава: сделок по нему нет, исполнение его больше не ведёт."""
        key = instrument_key(instrument)
        for target in self._instrument_lists():
            target[:] = [item for item in target if instrument_key(item) != key]
        self.execution_aliases = {alias: value for alias, value in self.execution_aliases.items() if value != key}

    def _instrument_lists(self):
        lists = [self.execution_instruments]
        if self.instruments is not None:
            lists.append(self.instruments)
        lifecycle = self.contract_lifecycle
        if lifecycle is not None and not getattr(lifecycle, "automatic", False) and hasattr(lifecycle, "instruments"):
            lists.append(lifecycle.instruments)
        return [target for index, target in enumerate(lists) if all(target is not other for other in lists[:index])]


def _build_runtime(
    instruments, data_cache, bus, state_dir, clock=None, session=None, client_provider=None, cluster_gate=None,
    contracts=None,
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
    historical = isinstance(clock, HistoricalClock)
    if not trading_enabled() and not historical:
        log.info("Торговый режим выключен — NotifyOnly.")
        return _Runtime()

    from src.broker import create_addressable_journal_broker
    from src.trade_journal.storage import Storage
    from src.trade_management.manager import TradeManager
    from src.trade_management.notification import stop_move_fields

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
        if historical:
            raise RuntimeError(f"Не удалось создать исторический симулятор/журнал: {exc}") from exc
        log.warning("Не удалось поднять SQLite-симуляцию (%s) — NotifyOnly.", exc)
        return _Runtime()

    risk_limits = _risk_limits()
    names = _instrument_names(instruments)
    costs = {"commission": 1.5, "slippage": 1.0} if historical else {}

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
             # Перенос стопа: прежний и новый уровни, остаток и оценка до стопа —
             # явными полями, чтобы шаблоны не разбирали снимок графика.
             **(stop_move_fields(details["visual"].data)
                if event_type is EventType.STOP_MOVED and details.get("visual") else {}),
             **{key: details[key] for key in ("gross_pnl", "net_pnl", "fees_total", "fees_known", "pnl_units", "quantity_remaining", "target_number",
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
        commission=RISK_LIMITS.get("commission", costs.get("commission")),
        slippage=RISK_LIMITS.get("slippage", costs.get("slippage")),
        slippage_tolerance=RISK_LIMITS.get("slippage_tolerance"),
        min_trade_risk_pct=RISK_LIMITS.get("min_trade_risk_pct"),
        portfolio_pct=RISK_LIMITS.get("portfolio_pct", 2),
        min_risk_cost_ratio=RISK_LIMITS.get("min_risk_cost_ratio", 2),
        min_net_payoff=RISK_LIMITS.get("min_net_payoff", 1.5),
        max_slippage_r=RISK_LIMITS.get("max_slippage_r", 0.25),
        max_entry_deviation_r=RISK_LIMITS.get("max_entry_deviation_r", 0.25),
        live_market_provider=None if historical else data_cache.entry_market if getattr(data_cache, "live_session_enabled", False) is True else None,
        contract_expiry_block_days=CONTRACT_EXPIRY_BLOCK_DAYS,
        contract_expiry_close_before_end_min=CONTRACT_EXPIRY_CLOSE_BEFORE_END_MIN,
        direction_limits=TRADING_DIRECTIONS,
        signal_filter=SignalFilter(),
        clock=clock,
        cluster_gate=cluster_gate,
        intraday_settings=INTRADAY_SETTINGS,
        session_calendar=_session_calendar_provider(data_cache),
    )
    if historical:
        trade_manager.initialize_account()
    trade_manager.restore()
    # Live: общий outbox доставляется только по read-only рыночным фактам
    # owner (design 9a). Начало торгового шага отличает текущее намерение
    # от удержанного раньше, актуальность которого не доказана.
    intent_since = [None]
    live_dispatch = not historical and getattr(data_cache, "live_session_enabled", False) is True
    if live_dispatch:
        trade_manager.configure_dispatch_eligibility(
            _dispatch_evidence(data_cache, instruments, storage, intent_since), lambda: data_cache._lock)
    trade_manager.configure_visual_context(instruments)
    action_executor = _OutboxExecutor(trade_manager)
    selection = getattr(session, "contract_selection", None)
    if isinstance(selection, HistorySelection) and selection.families:
        from src.portfolio.models import ContractMeta
        contracts = _load_contracts_metadata(selection.fixed, client_provider=client_provider)
        for chain in selection.chains:
            for segment in chain:
                card = segment.card
                contracts[card.uid] = ContractMeta(card.uid, float(card.price_step), float(card.step_cost),
                    float(card.go_buy), float(card.go_sell), currency=(card.currency or "RUB").upper(), expiration_date=card.expiration_date)
    elif contracts is None:
        contracts = _load_contracts_metadata(instruments, client_provider=client_provider,
                                             request_budget=getattr(data_cache, "_request_budget", None))
    broker.set_contracts(contracts)
    storage.set_contract_metadata(contracts)
    storage.set_names(_instrument_names(instruments))
    broker.set_names(_instrument_names(instruments))
    print_contract_metadata(contracts, _instrument_names(instruments))
    lifecycle = ConstantContractLifecycle(instruments)
    if isinstance(selection, HistorySelection) and selection.families:
        lifecycle = RolloverLifecycle(selection, storage, trade_manager, data_cache, _family_warmup, session.end,
            work_timeframes=lambda instrument: {pair.timeframe for pair in active_pairs([instrument], SHARE_STRATEGIES, FUTURE_STRATEGIES)})
        trade_manager.contract_lifecycle = lifecycle
        broker.increase_allowed = lifecycle.execution_increase_allowed
        lifecycle.configure_cache()
        session.contract_lifecycle = lifecycle

    # Cursors advance only after executions and observations of a minute have
    # committed. On restart SQLite provides the safe boundary of live state.
    execution_cursors = storage.execution_bar_boundaries()
    execution_closes = {}
    pending_bar = None
    # Live owner: каждая исполняемая минута закрепляется до симуляции, а после
    # рестарта рыночный marker догоняет уже durable финансовую границу.
    pin_input = getattr(data_cache, "pin_execution_input", None)
    pinned_execution = getattr(data_cache, "live_session_enabled", False) is True and callable(pin_input)
    resumed_markers = set()

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
            instrument = next((item for item in instruments if instrument_key(item) == ticker), None)
            mark_processed = getattr(data_cache, "mark_processed", None)
            if instrument is not None and callable(mark_processed):
                mark_processed(instrument, "1m", stamp)

    runtime_ref: list = []

    def journal_ids(key):
        """Ключ инструмента и его псевдонимы, под которыми сделки лежат в журнале."""
        aliases = (getattr(runtime_ref[0], "execution_aliases", None) or {}) if runtime_ref else {}
        canonical = aliases.get(key, key)
        return [canonical, *(alias for alias, target in aliases.items() if target == canonical and alias != canonical)]

    def on_bar(ready_tfs: set[str]) -> None:
        nonlocal pending_bar
        intent_since[0] = as_aware(trade_manager._clock.now())
        if live_dispatch:
            # Удержанный в своём шаге вход или добор больше не станет актуальным:
            # снимается до исполнения бара, не дожидаясь данных инструмента.
            try:
                trade_manager.cancel_undelivered_increases(intent_since[0])
            except Exception as exc:
                log.warning("Не удалось снять недоставленные входы и доборы: %s", exc)
        try:
            # The simulator has already advanced if a durable write failed.
            # Retry those same events before asking it to execute another bar.
            if pending_bar is not None:
                commit_bar(*pending_bar)
                pending_bar = None
            # Свечи минут, пройденных как «у источника нет»: только отметка в
            # журнале сделки, исполнение задним числом не переигрывается.
            drain_late = getattr(data_cache, "drain_late_source_absent", None)
            for uid, minute, ohlc in (drain_late() if callable(drain_late) else ()):
                trade_manager.record_late_candle(journal_ids(uid), minute, ohlc,
                                                 observed_at=trade_manager._clock.now())
            batches = {}
            initial_boundaries = None
            for instrument in lifecycle.execution_instruments():
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
                    ticker = instrument_key(instrument)
                    stamps = frame["datetime"].map(to_aware_utc)
                    boundary = execution_cursors.get(ticker)
                    if boundary is None:
                        if initial_boundaries is None:
                            initial_boundaries = storage.execution_bar_boundaries()
                        boundary = initial_boundaries.get(ticker, (stamps.max(), True))
                    last, inclusive = boundary
                    if pinned_execution and ticker not in resumed_markers:
                        resumed_markers.add(ticker)
                        if not inclusive:
                            data_cache.resume_execution_marker(instrument, last)
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
                    skipped = getattr(data_cache, "source_absent_between", None)
                    absent = skipped(instrument, last, fresh) if callable(skipped) and not fresh.empty else []
                    if absent:
                        # Пометка пропуска — до продвижения границы исполнения.
                        trade_manager.record_source_absent(journal_ids(ticker), absent,
                                                           observed_at=trade_manager._clock.now())
                    for row in fresh.itertuples(index=False):
                        stamp = to_aware_utc(row.datetime).to_pydatetime()
                        values = (float(row.open), float(row.low), float(row.high), float(row.close))
                        if pinned_execution:
                            try:
                                _, pinned = pin_input(instrument, row.datetime)
                            except ValueError as exc:
                                # Конфликт/integrity-hold: не выдумываем OHLC и
                                # не перескакиваем минуту; healthy UID идут дальше.
                                log.warning("Исполнение %s удержано перед %s: %s",
                                            _instrument_ticker(instrument), stamp, exc)
                                break
                            values = tuple(float(pinned[key]) for key in ("open", "low", "high", "close"))
                        batches.setdefault(stamp, {})[ticker] = values
                except Exception as exc:
                    log.warning(
                        "Сбой подготовки минутных баров по %s: %s",
                        _instrument_ticker(instrument), exc,
                    )
                    continue
            for stamp, prices in sorted(batches.items()):
                instrument_bar_times = {ticker: stamp for ticker in prices}
                try:
                    broker.track_bar(stamp, prices, contracts, bar_times=instrument_bar_times)
                    addressed = list(broker.drain_addressed_events())
                    broker_events = list(broker.drain_events())
                except Exception as exc:
                    # track_bar may have mutated the in-memory simulator. It is
                    # unsafe to retry another bar until SQLite rehydrates it.
                    raise _UnsafeExecutionState("Сбой симуляции до фиксации минуты") from exc
                pending_bar = (stamp, prices, addressed, broker_events)
                commit_bar(*pending_bar)
                pending_bar = None
            return True
        except _UnsafeExecutionState:
            log.exception("Исполнение остановлено до восстановления из SQLite")
            raise
        except Exception as exc:
            log.warning("Сбой обработки бара исполнением: %s", exc)
            return False

    runtime = _Runtime(
        post_tick=on_bar,
        trade_manager=trade_manager,
        action_executor=action_executor,
        storage=storage,
        contract_lifecycle=lifecycle,
    )
    runtime.instruments = instruments

    def accept_contracts(metas):
        # Тот же словарь видит исполнение по барам: карточка нового контракта — сразу в работе.
        contracts.update(metas)
        broker.set_contracts(metas)
        storage.set_contract_metadata(metas)
        names = _instrument_names(runtime.execution_instruments or instruments)
        storage.set_names(names)
        broker.set_names(names)
        trade_manager.configure_visual_context(runtime.execution_instruments or instruments)
    runtime.on_contracts = accept_contracts
    runtime_ref.append(runtime)
    return runtime


def _session_calendar_provider(data_cache):
    """Расписание режима торгов инструмента для сессионной защиты: только сохранённые дни.

    Live и исторический прогон используют один и тот же ``MarketCalendar``
    кэша (в истории — расписание снимка прогона); сеть на тике не трогается.
    """
    def provider(instrument, now):
        calendar = getattr(data_cache, "market_calendar", None)
        uid = data_cache.instrument_uid(instrument)
        if calendar is None or not uid:
            return None
        # Восстановленные дни прогона — только для сессионной защиты; календарь
        # отдаёт их, лишь если прогон их разрешил ([historical.schedule]).
        return calendar.calendar_for(uid, now, include_reconstructed=True)
    return provider


def _dispatch_evidence(data_cache, instruments, storage, intent_since):
    """Готовность входа для допуска доставки; без сети и без stream-reader.

    Вызывается в финансовом потоке под guard кэша: срез качества не меняется
    между проверкой и локальной доставкой.
    """
    from src.trade_management.manager import DispatchDataEvidence, created_before_step

    def inspect(instrument_id, action, command):
        # Список живой: инструмент, добавленный в состав на ходу, тоже получает допуск доставки.
        instrument = next((item for item in instruments if instrument_key(item) == instrument_id), None)
        if instrument is None:
            return None
        execution = data_cache.pair_status(instrument, "1m")["state"] == "READY"
        row = storage.connection.execute("SELECT plan_json FROM trades WHERE trade_id=?", (command.trade_id,)).fetchone()
        timeframe = json.loads(row[0]).get("timeframe", "") if row else ""
        work = bool(timeframe) and data_cache.pair_status(instrument, timeframe)["state"] == "READY"
        since = intent_since[0]
        current = True if since is not None and not created_before_step(command.created_at, since) else None
        return DispatchDataEvidence(execution, work, current)

    return inspect


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




def _instrument_names(instruments) -> dict[str, str]:
    """Карта адрес контракта (UID/legacy ticker) → короткое имя.

    В карту попадают только настоящие короткие имена: тикер и полный label
    пользователю не показываются, для неизвестного контракта остаётся
    ``контракт не указан``.
    """
    names: dict[str, str] = {}
    for instrument in instruments:
        ticker = _instrument_ticker(instrument) if isinstance(instrument, (tuple, list)) else instrument_key(instrument)
        if not ticker:
            continue
        if isinstance(instrument, (tuple, list)):
            short = instrument[3] if len(instrument) > 3 else None
        else:
            short = getattr(instrument, "short_name", None)
        if short:
            names[ticker] = short
    return names








def print_contract_metadata(contracts, names=None) -> None:
    for ticker, meta in sorted(contracts.items()):
        log.info(
            "Контракт %s: шаг %.6f, стоимость шага %.4f, ГО(купли) %.2f, ГО(продажи) %.2f",
            (names or {}).get(ticker) or "контракт не указан", meta.price_step, meta.step_cost, meta.go_buy, meta.go_sell,
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


def build_demand(runtime, instruments, data_cache) -> ContourDemand:
    """Потребности контура для рыночных служб; финансовая база читается главным потоком.

    Состав учитывает роли жизненного цикла контрактов: инструменты исполнения
    (в том числе DRAINING с обязательствами) и рынка, без автоматического
    rollover. Алиасы и инструменты исполнения остаются у торговой части контура.
    """
    lifecycle = getattr(runtime, "contract_lifecycle", None)
    roster = list(instruments)
    for source in ("execution_instruments", "market_instruments"):
        extra = getattr(lifecycle, source, None)
        for instrument in (extra() if callable(extra) else ()):
            if instrument_key(instrument) not in {instrument_key(item) for item in roster}:
                roster.append(instrument)
    patterns = _pattern_pairs(instruments)
    storage = getattr(runtime, "storage", None)
    if storage is None:
        return ContourDemand(tuple(roster), pattern_pairs=patterns)
    trades = storage.load_trades()
    aliases = {alias: instrument_key(instrument)
               for instrument in roster
               for alias in (instrument_key(instrument), instrument.ticker)}
    runtime.execution_aliases = aliases
    runtime.execution_instruments = list(roster)
    journal = storage.execution_bar_boundaries()
    boundaries = _canonical_execution_boundaries(journal, aliases)
    requests, lowers = [], []
    for instrument in roster:
        key = instrument_key(instrument)
        for timeframe, count in obligation_requirements(
                [t for t in trades if aliases.get(t.plan.instrument_id) == key]).items():
            requests.append((instrument, timeframe, count))
        if key in boundaries:
            lowers.append((instrument, boundaries[key][0]))
    return ContourDemand(tuple(roster), aliases, _market_obligations(journal, aliases, roster, data_cache),
                         tuple(requests), tuple(lowers), patterns)




@dataclass(frozen=True)
class ContourSettings:
    """То, что различается между контурами; общие настройки робота — в ``src.config``."""

    kind: str
    state_dir: Path
    channel_names: list | None
    clock: object
    session: object
    client_provider: object
    instruments: list
    market_data_sync: MarketDataSync | None = None
    # Карточки контрактов от ядра (launch.json); None — контур грузит их сам (история).
    contracts: dict | None = None
    # Желаемое состояние ядра (add-control-api); None — процесс без управления (история).
    control: object = None


class TradingContour:
    """Торговый контур: каналы, кластерный вердикт, торговая часть, оркестратор, отчёт истории.

    Данные приходят только через порт ``MarketServices``; контур не знает,
    живые они (ядро) или исторические (эмулятор).
    """

    def __init__(self, settings: ContourSettings, services) -> None:
        self.settings = settings
        self.services = services
        self.channels: list = []
        self.bus = None
        self.runtime = None

    def build(self) -> "TradingContour":
        settings, services = self.settings, self.services
        # Отметки исполнения в рыночной базе пишутся под ключом контура.
        if hasattr(services.data_cache, "contour_id"):
            services.data_cache.contour_id = settings.kind
        self.channels = build_channels(settings.channel_names, state_dir=settings.state_dir)
        self.bus = EventBus()
        self.bus.subscribe_all(self.channels)
        htf_provider = HtfFrameProvider(cache=services.data_cache, timeline=services.timeline)
        PROFILES["triple_screen"] = TripleScreenFilter(provider=htf_provider, params=TRIPLE_SCREEN_PARAMS)
        cluster_gate = services.cluster_gate(settings.state_dir)
        self.runtime = _build_runtime(
            settings.instruments, services.data_cache, self.bus, settings.state_dir, clock=settings.clock,
            session=settings.session, client_provider=settings.client_provider, cluster_gate=cluster_gate,
            contracts=settings.contracts,
        )
        return self

    def demand(self) -> ContourDemand:
        return build_demand(self.runtime, self.settings.instruments, self.services.data_cache)

    def run(self, live_waiter=None) -> None:
        services = self.services
        _run_bot(self.settings.instruments, self.bus, self.runtime, services.data_cache, services.timeline,
                 session=self.settings.session, live_waiter=live_waiter,
                 update_obligations=services.update_obligations if services.tracks_obligations else None,
                 after_tick=services.acknowledge_tick, control=self.settings.control,
                 forget=getattr(services, "forget_instrument", None))

    def finish(self) -> None:
        """Отчёт исторического прогона — и для штатного, и для аварийного финиша."""
        storage = None if self.runtime is None else self.runtime.storage
        _finish_history(storage, self.settings.session, self.services.data_cache, self.services.timeline,
                        self.settings.state_dir, self.settings.market_data_sync)

    def close(self) -> None:
        close_channels(self.channels)
