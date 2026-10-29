from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

from src.api.retry import DEFAULT_BASE_DELAY, _is_rate_limited, rate_limit_reset_secs
from src.bot.run_session import LiveRunSession
from src.config import BAR_TIME_TZ_OFFSET_HOURS, TIMEFRAMES
from src.data.ingestion_settings import MarketDataSettings
from src.instruments import Instrument, normalize_instrument
from src.instruments.model import instrument_key
from src.events.event import Event
from src.events.visual import CANDLE_LIMIT
from src.trade_management.notification import signal_snapshot
from src.logging_setup import correlation_id_var, get_logger
from src.market_context.lazy import LazyMarketContext
from src.notifier.errors import is_rate_limit
from src.scheduler.clock import as_aware
from src.strategies.contracts import Assignment, SignalType
from src.strategies.registry import get_strategy, validate_assignments
from src.trade_management.actions import AddToTrade, TradeAction
from src.history.contract_lifecycle import ConstantContractLifecycle, ContractLifecycleError
from src.history.warmup import obligation_requirements

log = get_logger(__name__)

_DECISION_OUTCOMES = {
    SignalType.BUY: "signal_buy",
    SignalType.SELL: "signal_sell",
}


class _OperationError(Exception):
    """Несёт человекочитаемое имя операции, на которой случился сбой.

    Исходное исключение доступно как ``cause`` — его текст уходит в журнал,
    а имя операции — в уведомление пользователя.
    """

    def __init__(self, operation: str, cause: Exception) -> None:
        super().__init__(operation)
        self.operation = operation
        self.cause = cause


@dataclass(frozen=True)
class _EntryCandidate:
    assignment: Assignment
    decision: object
    instrument: Instrument
    frame: object
    context: object
    timeframe: str

    def sort_key(self) -> tuple[int, str, str, str]:
        return (
            -self.assignment.priority,
            self.assignment.id,
            instrument_key(self.instrument),
            self.decision.event_id or "",
        )


def assignments_for(instrument: Instrument, share_strategies, future_strategies) -> list[Assignment]:
    """Привязки инструмента: акция — по тикеру, фьючерс — по семейству."""
    if instrument.instrument_type == "future":
        if isinstance(instrument.family, str):
            from src.instruments.futures import FAMILIES
            return future_strategies.get(instrument.family,
                future_strategies.get(FAMILIES[instrument.family][0], []))
        return future_strategies.get(instrument.ticker[:2].upper(), [])
    return share_strategies.get(instrument.ticker, [])


class TradingBot:
    """Оркестратор-сценарий: каждый этап работы робота — отдельный метод.

    Ритм — «один тик = одна закрытая свеча»: итерация выравнивается по границе
    закрытия свечи таймфрейма. Решения принимаются только по готовым (закрытым)
    свечам, а уведомление выполняется на каждом тике по каждой активной паре
    «инструмент × стратегия».
    """

    def __init__(
        self,
        instruments,
        bus,
        strategy_map: dict[str, object],
        data_cache,
        timeline,
        strategy_factory=get_strategy,
        share_strategies: dict[str, list[Assignment]] | None = None,
        future_strategies: dict[str, list[Assignment]] | None = None,
        heartbeat_every_ticks: int | None = 60,
        tick_poll_secs: float = 1.0,
        tick_timeout_secs: float = 65.0,
        context_cache=None,
        signal_filter=None,
        post_tick=None,
        trade_manager=None,
        action_executor=None,
        protection_timeframe: str = "1m",
        run=None,
        contract_lifecycle=None,
        market_data_settings=None,
        live_data_waiter=None,
        poll=None,
    ) -> None:
        self._bus = bus
        # Работа между тиками (add-tinvest-orders): исполнения брокера разбираются не реже опроса.
        self._poll = poll
        self._strategy_map = strategy_map
        self._data_cache = data_cache
        self._timeline = timeline
        self._run = run if run is not None else LiveRunSession()
        self._strategy_factory = strategy_factory
        # Тот же объект словаря: контур на брокере дополняет его привязками на ходу (add-tinvest-orders).
        self._share_strategies = share_strategies if share_strategies is not None else {}
        self._future_strategies = future_strategies if future_strategies is not None else {}
        self._heartbeat_every = heartbeat_every_ticks or 0
        self._tick_poll_secs = tick_poll_secs
        self._tick_timeout_secs = tick_timeout_secs
        # Live-ожидание публикации и повтор статуса настраиваются отдельно от
        # legacy-таймаута такта (design 7a).
        self._market_data_settings = market_data_settings or MarketDataSettings()
        # Ограниченное ожидание первичного прогрева фоновой ingestion-полосой.
        self._await_live_data = live_data_waiter
        self._context_cache = context_cache
        self._signal_filter = signal_filter
        self._post_tick = post_tick
        self._trade_manager = trade_manager
        self._action_executor = action_executor
        self._protection_timeframe = protection_timeframe
        # Последний переданный менеджеру кадр графика по паре: (бар, длина).
        self._remembered_markets: dict[tuple[str, str], tuple] = {}

        self._instruments = [
            i if isinstance(i, Instrument) else normalize_instrument(i)
            for i in instruments
        ]
        self._contract_lifecycle = contract_lifecycle or ConstantContractLifecycle(self._instruments)
        self._tick_count = 0
        self._errors_in_period = 0
        # Живой анализ — для изменения состава контура на ходу (add-contour-membership).
        self._live_analysis = None
        self._heartbeat_countdown = self._heartbeat_every

    # ── ПУНКТ 1: запуск ──
    def run(self) -> None:
        self._validate()
        log.info("Робот запущен. Ctrl+C для остановки.")
        self._loop()

    # ── ПУНКТ 2: бесконечный цикл «тик = закрытая свеча активного ТФ» ──
    def _loop(self) -> None:
        if getattr(self._data_cache, "live_session_enabled", False) is True:
            return self._loop_live()
        first = True
        while not self._run.should_stop():
            try:
                if first:
                    ready_tfs = self._bootstrap()
                else:
                    ready_tfs = self._timeline.wait_until_bar_published(
                        self._bar_is_ready,
                        poll_secs=self._tick_poll_secs,
                        timeout_secs=self._tick_timeout_secs,
                    )
                self._tick(ready_tfs)
                first = False
            except KeyboardInterrupt:
                log.info("Бот остановлен.")
                return
            except ContractLifecycleError as exc:
                self._run.mark_contract_failure(str(exc))
                return
            except Exception as exc:
                if getattr(exc, "fatal_execution", False):
                    raise
                self._report_error(exc, "обработка тика")
                if _is_rate_limited(exc):
                    reset = rate_limit_reset_secs(exc) or DEFAULT_BASE_DELAY
                    delay = min(self._timeline.fallback_secs(), reset)
                    log.warning("Rate limit (обработка тика). Ожидание %ds...", delay)
                    time.sleep(delay)
                else:
                    time.sleep(self._timeline.fallback_secs())
        log.info("Прогон завершён: %s", self._run.stop_reason())

    def _loop_live(self):
        from src.bot.live_analysis import LiveAnalysis
        live = LiveAnalysis(self)
        self._live_analysis = live
        try:
            live.bootstrap()
            changed = getattr(self._data_cache, "wait_for_data_change", None)
            while not self._run.should_stop():
                # Поколение снимается до шага: данные, пришедшие во время
                # обработки, разбудят следующий шаг без ожидания опроса.
                generation = getattr(self._data_cache, "data_generation", None)
                try:
                    live.step()
                except Exception as exc:
                    if getattr(exc, "fatal_execution", False):
                        raise
                    self._report_error(exc, "обработка live-пар")
                if self._poll is not None:
                    try:
                        self._poll()
                    except Exception as exc:
                        self._report_error(exc, "работа контура между тиками")
                waiter = (lambda timeout: changed(generation, timeout)) if callable(changed) and generation is not None else None
                self._timeline.wait_live_poll(self._tick_poll_secs, data_waiter=waiter, wake_at=live.next_wakeup())
        except KeyboardInterrupt:
            log.info("Бот остановлен.")

    # ── ПУНКТ 2.0: первый тик при запуске без ожидания границы ──
    def _bootstrap(self) -> set[str]:
        """Первичное наполнение кадров и ограниченное ожидание свежего закрытого бара.

        Не дожидается ближайшей календарной границы: сразу анализирует последнюю
        уже закрытую свечу каждого активного ТФ. Если свежий закрытый бар ещё не
        опубликован (запуск на границе), ждёт его появления с тем же таймаутом,
        что и штатный тик. Возвращает ТФ с готовой свежей свечой.
        """
        bootstrap_instruments = self._contract_lifecycle.market_instruments() if self._contract_lifecycle.automatic else self._instruments
        for instrument in bootstrap_instruments:
            for tf in self._assigned_timeframes(instrument):
                self._data_cache.frame_for(instrument, tf)
        return self._timeline.wait_until_bar_published(
            self._bar_is_ready,
            poll_secs=self._tick_poll_secs,
            timeout_secs=self._tick_timeout_secs,
            wait_boundary=False,
        )

    # ── ПУНКТ 3: один тик — обновить данные и обработать инструменты ──
    def _tick(self, ready_tfs: set[str]) -> None:
        tick_id = uuid4().hex[:8]
        correlation_id_var.set(tick_id)
        completed = False
        self._bus.publish(Event.tick_started(tick_id=tick_id))
        try:
            if self._contract_lifecycle.automatic:
                self._contract_lifecycle.before_tick(self._timeline.now())
                self._instruments = list(self._contract_lifecycle.market_instruments())
            for instrument in self._instruments:
                for tf in self._assigned_timeframes(instrument):
                    try:
                        self._data_cache.frame_for(instrument, tf)
                    except Exception as exc:
                        self._report_error(exc, f"обновление данных {instrument.label} ({tf})")
            for tf in ready_tfs:
                try:
                    self._data_cache.refresh_if_new_candle(tf)
                except Exception as exc:
                    self._report_error(exc, f"обновление свечей таймфрейма {tf}")
            self._data_cache.close_tick()
            if self._run.is_virtual and callable(getattr(self._data_cache, "final_drivers_processed", None)):
                self._run.update_data_status(source_failed=self._data_cache.history_source_failed,
                                             drivers_complete=self._data_cache.final_drivers_processed())
            calendar = getattr(self._data_cache, "take_calendar_event", None)
            event = calendar() if self._contract_lifecycle.automatic and callable(calendar) else None
            if event is not None:
                self._run.clock.jump_to(event)
                self._timeline.reanchor()
                if getattr(self._run, "boundary_checks", ()):
                    self._contract_lifecycle.before_tick(event)
                    self._contract_lifecycle.after_execution(event)
                return
            terminal = getattr(self._data_cache, "take_terminal_gap", None)
            completion = terminal() if self._run.is_virtual and callable(terminal) else None
            if completion is not None and self._run.complete_terminal_gap(completion):
                self._timeline.reanchor()
                return
            if getattr(self._data_cache, "data_exhausted", False):
                self._run.mark_data_exhausted(
                    getattr(self._data_cache, "data_exhaustion", None)
                )
            gap = self._take_data_gap()
            if gap is not None:
                # Разрыв в данных: рыночное время переведено на следующий имеющийся
                # бар, тик ничего не принёс, поэтому обработка бара на этом шаге
                # не делается — следующий тик начнётся уже за разрывом.
                self._run.mark_data_gap(gap)
                self._timeline.reanchor()
                return
            # Protection is simulated from closed base bars before any strategy
            # may calculate a new entry. One lagging pair must not block the
            # available minutes of other instruments behind the global TF gate.
            if self._post_tick is not None:
                execution_complete = self._post_tick({self._protection_timeframe}) is not False
            else:
                execution_complete = True
            if self._contract_lifecycle.automatic:
                self._contract_lifecycle.after_execution(self._timeline.now())
                self._instruments = list(self._contract_lifecycle.market_instruments())
            note_processed = getattr(self._data_cache, "note_driver_processed", None)
            if self._run.is_virtual and execution_complete and callable(note_processed):
                note_processed()
                self._run.update_data_status(source_failed=self._data_cache.history_source_failed,
                                             drivers_complete=self._data_cache.final_drivers_processed())
            if not ready_tfs:
                return
            entry_candidates: list[_EntryCandidate] = []
            for instrument in self._instruments:
                try:
                    self._process(instrument, ready_tfs, entry_candidates)
                except _OperationError as err:
                    self._report_error(err.cause, err.operation)
                except Exception as exc:
                    self._report_error(exc, f"анализ {instrument.label}")
            for candidate in sorted(entry_candidates, key=_EntryCandidate.sort_key):
                try:
                    # Решение публикуется перед своим допуском: ПЛАН/ОТКЛОНЕНО
                    # идут в консоли одним блоком с СИГНАЛ.
                    self._emit(candidate.instrument, candidate.assignment.strategy, candidate.decision,
                               filter_profile=candidate.assignment.filter_profile,
                               filtered_out=False, timeframe=candidate.timeframe)
                    self._admit_candidate(candidate)
                except _OperationError as err:
                    self._report_error(err.cause, err.operation)
                except Exception as exc:
                    self._report_error(exc, f"портфельный допуск {candidate.instrument.label}")
            self._maybe_heartbeat()
            completed = True
        finally:
            self._bus.publish(Event.tick_finished(tick_id=tick_id, completed=completed))
            correlation_id_var.set(None)

    def _lazy_context(self, instrument: Instrument, tf: str, frame) -> LazyMarketContext:
        """Контекст ТФ считается, только если его прочтут фильтр или сопровождение."""
        name = self._display_name(instrument)
        return LazyMarketContext(
            lambda: self._context_cache.get_context(instrument, tf, frame),
            on_load=lambda context: log.debug("%s %s | контекст: %s", name, tf, context),
        )

    def _take_data_gap(self):
        """Забирает найденный кэшем разрыв в данных (одноразово, ``None`` — разрыва нет)."""
        take = getattr(self._data_cache, "take_pending_gap", None)
        if not callable(take):
            return None
        return take()

    # ── ПУНКТ 2.1: готов ли свежий закрытый бар ТФ (для ожидания до появления) ──
    def _bar_is_ready(self, timeframe: str) -> bool:
        if self._data_cache.has_fresh_closed_bar(timeframe):
            return True
        self._data_cache.refresh_if_new_candle(timeframe, force=True)
        return self._data_cache.has_fresh_closed_bar(timeframe)

    # ── ПУНКТ 4: по инструменту — только привязки ТФ с закрывшейся свечой ──
    def _process(
        self,
        instrument: Instrument,
        ready_tfs: set[str],
        entry_candidates: list[_EntryCandidate] | None = None,
    ) -> None:
        assignments = self._strategies_for(instrument)
        assigned_tfs = {assignment.timeframe for assignment in assignments}
        # Защитный 1m нужен действиям, не связанным с баром (таймаут входа,
        # экспирация); профиль на нём не считается — менеджер сопровождает
        # сделку только кадром таймфрейма её плана.
        management_needs = set(self._management_needs(instrument)) if self._trade_manager is not None else set()
        management_tfs = (
            {self._protection_timeframe} | management_needs
            if self._trade_manager is not None else set()
        )
        for tf in sorted(
            (assigned_tfs | management_tfs) & ready_tfs,
            key=lambda timeframe: (timeframe != self._protection_timeframe, timeframe),
        ):
            frame = self._data_cache.frame_for(instrument, tf)
            if frame.empty:
                log.info("Нет готовых свечей для %s (%s) — пропускаем.", self._display_name(instrument), tf)
                continue
            context = self._lazy_context(instrument, tf, frame) if self._context_cache else None
            tf_assignments = [a for a in assignments if a.timeframe == tf]
            log.debug(
                "%s %s | получено свечей=%d, последний бар O=%.4f H=%.4f L=%.4f C=%.4f",
                self._display_name(instrument), tf, len(frame),
                frame["open"].iloc[-1], frame["high"].iloc[-1],
                frame["low"].iloc[-1], frame["close"].iloc[-1],
            )
            if tf_assignments or tf in management_needs:
                self._remember_market(instrument, tf, frame)
            self._manage(instrument, tf_assignments, frame, context, tf)
            if tf_assignments:
                self._analyze(instrument, tf_assignments, frame, context, tf, entry_candidates)

    # ── ПУНКТ 4.2: анализ по каждой привязке «стратегия × профиль» одного ТФ ──
    def _analyze(
        self,
        instrument: Instrument,
        assignments: list[Assignment],
        frame,
        context=None,
        tf: str = "",
        entry_candidates: list[_EntryCandidate] | None = None,
    ) -> None:
        summaries = []
        for assignment in assignments:
            name = assignment.strategy
            try:
                strategy = self._strategy_cache.get((name, tf))
                if strategy is None:
                    log.warning(
                        "Стратегия '%s' не построена для %s (%s) — пропускаем.",
                        name, self._display_name(instrument), tf,
                    )
                    continue
                ta = strategy.compute(frame)
                decision = strategy.decide(ta, timeframe=tf)
                if self._contract_lifecycle.automatic and not self._contract_lifecycle.signal_allowed(
                        instrument, frame["datetime"].iloc[-1], decision.bar_time, self._timeline.now()):
                    continue
                log.debug(
                    "%s %s | %s → %s %s",
                    self._display_name(instrument), tf, name,
                    decision.signal_type.name,
                    self._ta_digest(ta),
                )
                bar_time = self._timeline.grid(tf).bar_close(frame["datetime"].iloc[-1])
                msk_time = bar_time + timedelta(hours=BAR_TIME_TZ_OFFSET_HOURS)
                event_id = (
                    f"{assignment.id}:{instrument_key(instrument)}:{tf}:"
                    f"{msk_time.isoformat()}:{decision.signal_type.value}"
                )
                decision = replace(
                    decision,
                    bar_time=bar_time,
                    available_at=decision.available_at or bar_time,
                    event_id=event_id,
                )
                if self._trade_manager is not None:
                    actions_for_signal = getattr(self._trade_manager, "actions_for_signal", None)
                    if actions_for_signal is not None:
                        candidate = _EntryCandidate(
                            assignment, decision, instrument, frame, context, tf,
                        )
                        if entry_candidates is None:
                            self._emit(instrument, name, decision,
                                filter_profile=assignment.filter_profile,
                                filtered_out=False, timeframe=tf)
                            self._admit_candidate(candidate)
                        else:
                            entry_candidates.append(candidate)
                        summaries.append(f"{name}={decision.signal_type.name}")
                        continue
                filtered_out = False
                if context is not None:
                    if self._signal_filter is not None:
                        raw = decision
                        decision = self._signal_filter.apply(
                            decision,
                            context,
                            profile_name=assignment.filter_profile,
                            instrument=instrument,
                            timeframe=tf,
                        )
                        filtered_out = (
                            raw.signal_type is not SignalType.HOLD
                            and decision.signal_type is SignalType.HOLD
                        )
                        if filtered_out:
                            log.debug(
                                "%s %s | %s [%s]: сигнал %s отклонён фильтром",
                                self._display_name(instrument), tf, name,
                                assignment.filter_profile, raw.signal_type.name,
                            )
                self._emit(
                    instrument, name, decision,
                    filter_profile=assignment.filter_profile,
                    filtered_out=filtered_out,
                    timeframe=tf,
                )
                summaries.append(f"{name}={decision.signal_type.name}")
            except (ValueError, TypeError, KeyError) as exc:
                log.warning(
                    "Проблема со стратегией '%s' на %s: %s", name, self._display_name(instrument), exc
                )
            except Exception as exc:
                raise _OperationError(
                    f"анализ {self._display_name(instrument)} ({tf}, {name})", exc
                ) from exc
        if summaries:
            log.info(
                "%s %s | итог: %s",
                self._display_name(instrument), tf, ", ".join(summaries),
            )

    def _admit_candidate(self, candidate: _EntryCandidate) -> None:
        """Apply trade-manager admission after every tick candidate is known."""
        actions_for_signal = getattr(self._trade_manager, "actions_for_signal", None)
        if actions_for_signal is None:
            return
        admission = actions_for_signal(
            candidate.assignment,
            candidate.decision,
            candidate.instrument,
            candidate.frame,
            candidate.context,
            timeframe=candidate.timeframe,
        )
        stats = getattr(self._run, "admission_stats", None)
        if stats is not None:
            stats.record(candidate.assignment, candidate.timeframe, candidate.decision, admission)
        diagnostics = dict(admission.diagnostics)
        if admission.rejections and not diagnostics:
            provider = getattr(self._trade_manager, "portfolio_diagnostics", None)
            if callable(provider):
                details = provider()
                if isinstance(details, Mapping):
                    diagnostics = dict(details)
        # Кластерный вердикт: строка «Кластеры» в блоке ПЛАН или ОТКЛОНЕНО.
        cluster = getattr(admission, "cluster", None)
        clusters = cluster.payload() if cluster is not None else None
        if admission.plan is not None:
            quantity = admission.actions[0].quantity if admission.actions else 0
            self._publish_signal(
                admission.plan,
                candidate.instrument,
                quantity=quantity,
                timeframe=candidate.timeframe,
                diagnostics=diagnostics,
                frame=candidate.frame,
                as_of=candidate.decision.bar_time,
                clusters=clusters,
            )
        self._dispatch_management_actions(
            admission.actions,
            candidate.decision,
            candidate.context,
            candidate.assignment,
            candidate.instrument,
            candidate.timeframe,
        )
        for reason in admission.rejections:
            self._publish_rejection(
                candidate.decision,
                candidate.instrument,
                reason=reason.message,
                code=reason.code,
                diagnostics=diagnostics,
                filter_profile=candidate.assignment.filter_profile,
                timeframe=candidate.timeframe,
                clusters=clusters,
            )

    def _manage(self, instrument: Instrument, assignments: list[Assignment], frame, context, tf: str) -> None:
        """Dispatch ongoing trade actions before evaluating new entry signals."""
        if self._trade_manager is None:
            return
        manage = getattr(self._trade_manager, "manage", None)
        if manage is None:
            return
        self._dispatch_management_actions(
            manage(instrument, assignments, frame, context, timeframe=tf),
            None,
            context,
            None,
            instrument,
            tf,
        )

    def _dispatch_management_actions(
        self, actions, entry_candidate, context, assignment: Assignment | None,
        instrument: Instrument, timeframe: str,
    ) -> None:
        """Send management actions directly; only adds need entry admission."""
        for action in actions or ():
            if not isinstance(action, TradeAction):
                raise TypeError("trade manager must return TradeAction instances")
            if isinstance(action, AddToTrade):
                if entry_candidate is None or assignment is None or self._signal_filter is None:
                    log.warning("Добор %s отклонён: нет входного кандидата или фильтра", action.trade_id)
                    continue
                candidate = self._signal_filter.apply(
                    entry_candidate,
                    context,
                    profile_name=assignment.filter_profile,
                    instrument=instrument,
                    timeframe=timeframe,
                )
                if candidate.signal_type is SignalType.HOLD:
                    continue
            executor = self._action_executor
            if executor is None:
                raise TypeError(
                    "action executor is required: orders are delivered through the outbox"
                )
            submit = getattr(executor, "submit", None)
            if submit is None:
                raise TypeError("action executor must support submit(action, now)")
            submit(action, as_aware(self._timeline.now()).replace(microsecond=0))

    # ── компактная сводка индикаторов по итоговому бару (для отладки) ──
    @staticmethod
    def _ta_digest(ta) -> str:
        if ta is None or len(ta) == 0:
            return ""
        last = ta.iloc[-1]
        interesting = [
            c for c in ta.columns
            if any(k in str(c).lower() for k in ("rsi", "stoch", "macd", "ema", "sma", "signal", "slope"))
        ]
        parts = []
        for c in interesting:
            v = last[c]
            try:
                parts.append(f"{c}={v:.3f}")
            except (TypeError, ValueError):
                parts.append(f"{c}={v}")
        return " | ".join(parts) if parts else ""

    # ── читаемое имя инструмента для логов (короткое «NG-10.26», не тикер и не label) ──
    @staticmethod
    def _display_name(instrument: Instrument) -> str:
        return instrument.short_name or "контракт не указан"

    # ── ПУНКТ 4.2.3-4.2.4: доставка через шину на каждом тике ──
    def _emit(self, instrument: Instrument, name: str, decision, *, filter_profile: str = "", filtered_out: bool = False, timeframe: str = "") -> None:
        self._publish_decision(
            decision,
            instrument,
            filter_profile=filter_profile,
            filtered_out=filtered_out,
            timeframe=timeframe,
        )

    def _publish_decision(
        self,
        decision,
        instrument: Instrument,
        *,
        filter_profile: str = "",
        filtered_out: bool = False,
        timeframe: str = "",
        analysis_mode: str = "",
    ) -> None:
        """Publish the strategy outcome; the channel decides how it reads."""
        if filtered_out:
            outcome = "filtered"
        else:
            outcome = _DECISION_OUTCOMES.get(decision.signal_type, "no_signal")
        self._bus.publish(
            Event.decision(
                self._display_name(instrument),
                outcome=outcome,
                side=decision.signal_type.name,
                price=decision.price,
                strategy=decision.strategy_name or "",
                filter_profile=filter_profile,
                filtered_out=filtered_out,
                bar_time=decision.bar_time,
                timeframe=timeframe,
                event_id=decision.event_id or "",
                analysis_mode=analysis_mode,
            )
        )

    def _remember_market(self, instrument, timeframe, frame) -> None:
        """Свечи пары для графиков исполнений — общий вход тикового и live-цикла.

        Перенос стопа и закрытие сделки рисуются по этому кадру менеджера;
        без него сообщение уходит текстом. Повтор того же кадра (тот же
        последний бар и длина) не пересчитывает снимок и разрывы. Сбой
        графика не мешает торговле.
        """
        remember = getattr(self._trade_manager, "remember_market", None)
        if not callable(remember) or frame is None or frame.empty or not timeframe:
            return
        last = frame["datetime"].iloc[-1]
        key, signature = (instrument_key(instrument), timeframe), (last, len(frame))
        if self._remembered_markets.get(key) == signature:
            return
        try:
            remember(instrument, timeframe, frame, self._timeline.grid(timeframe).bar_close(last),
                     breaks=self._chart_breaks(instrument, timeframe, frame))
        except Exception as exc:
            log.warning("График: свечи %s %s не переданы (%s).",
                        self._display_name(instrument), timeframe, type(exc).__name__)
            return
        self._remembered_markets[key] = signature

    def _chart_breaks(self, instrument, timeframe, frame):
        """Вид разрывов окна графика от слоя данных; сбой не мешает торговле."""
        classify = getattr(self._data_cache, "chart_breaks", None)
        if not callable(classify) or frame is None or len(frame) == 0 or not timeframe:
            return None
        try:
            breaks = classify(instrument, timeframe, frame.tail(CANDLE_LIMIT + 1))
        except Exception as exc:
            log.warning("График: разрывы окна не классифицированы (%s).", type(exc).__name__)
            return None
        return breaks if isinstance(breaks, Mapping) else None

    def _publish_signal(
        self,
        plan,
        instrument: Instrument,
        *,
        quantity: int = 0,
        timeframe: str = "",
        diagnostics=None,
        frame=None,
        as_of=None,
        clusters=None,
    ) -> None:
        """Publish the admitted plan: it is queued, not yet executed."""
        economics = plan.economics
        contract_for = getattr(getattr(self._trade_manager, "_broker", None), "contract_for", None)
        meta = contract_for(instrument_key(instrument)) if callable(contract_for) else None
        visual = signal_snapshot(plan, quantity, instrument, frame, as_of, meta, diagnostics,
                                 self._chart_breaks(instrument, plan.timeframe, frame))
        self._bus.publish(
            Event.signal(
                self._display_name(instrument),
                side=plan.side,
                quantity=quantity,
                entry=plan.reference_entry,
                stop=plan.stop_price,
                targets=tuple(target.price for target in plan.targets),
                expected_r=plan.expected_r,
                risk_amount=None if economics is None else economics.risk_amount,
                reward_amount=None if economics is None else economics.reward_amount,
                costs_amount=None if economics is None else economics.costs_amount,
                payoff_ratio=None if economics is None else economics.payoff_ratio,
                fixed_reward_amount=None if economics is None else economics.fixed_reward_amount,
                fixed_quantity=None if economics is None else economics.fixed_quantity,
                net_reward_amount=None if economics is None else economics.net_reward_amount,
                algorithm_version=plan.algorithm_version,
                diagnostics=diagnostics,
                strategy=plan.profile.name,
                timeframe=timeframe or plan.timeframe,
                trade_id=plan.trade_id,
                event_id=plan.signal_id,
                bar_time=as_of,
                visual=visual,
                clusters=clusters,
            )
        )

    def _publish_rejection(
        self,
        decision,
        instrument: Instrument,
        *,
        reason: str,
        code: str = "",
        diagnostics=None,
        filter_profile: str = "",
        timeframe: str = "",
        clusters=None,
    ) -> None:
        """Publish why the plan was not admitted."""
        self._bus.publish(
            Event.rejected(
                self._display_name(instrument),
                reason=reason,
                code=code,
                diagnostics=diagnostics,
                side=decision.signal_type.name,
                price=decision.price,
                strategy=decision.strategy_name or "",
                filter_profile=filter_profile,
                bar_time=decision.bar_time,
                timeframe=timeframe,
                event_id=decision.event_id or "",
                clusters=clusters,
            )
        )

    # ── диспетчеризация привязок по типу инструмента ──
    def _strategies_for(self, instrument: Instrument) -> list[Assignment]:
        return assignments_for(instrument, self._share_strategies, self._future_strategies)

    # ── ТФ и прогрев сопровождения незавершённых сделок инструмента ──
    def _management_needs(self, instrument: Instrument) -> dict[str, int]:
        """Таймфреймы планов незавершённых сделок с прогревом их профилей.

        Сделка сопровождается на таймфрейме своего плана, даже если привязок
        на нём в конфигурации не осталось; служебный 1m из требований не
        считается таймфреймом сопровождения, если на нём нет самой сделки.
        """
        obligations = getattr(self._trade_manager, "management_obligations", None)
        if not callable(obligations):
            return {}
        trades = obligations(instrument_key(instrument))
        planned = {trade.plan.timeframe for trade in trades if trade.plan.timeframe in TIMEFRAMES}
        return {tf: count for tf, count in obligation_requirements(trades).items() if tf in planned}

    # ── ТФ, задействованные привязками инструмента ──
    def _assigned_timeframes(self, instrument: Instrument) -> set[str]:
        timeframes = {assignment.timeframe for assignment in self._strategies_for(instrument)}
        timeframes.add(self._protection_timeframe)
        return timeframes

    # ── валидация привязок и построение стратегий до цикла (fail-fast) ──
    def _validate(self) -> None:
        validate_assignments(self._share_strategies, source="SHARE_STRATEGIES")
        validate_assignments(self._future_strategies, source="FUTURE_STRATEGIES")
        missing = sorted(
            {
                item.strategy
                for items in (
                    *self._share_strategies.values(),
                    *self._future_strategies.values(),
                )
                for item in items
            }
            - set(self._strategy_map)
        )
        if missing:
            raise ValueError(
                "Стратегии заявлены в привязках, но отсутствуют в карте стратегий: "
                + ", ".join(missing)
            )
        # форсируем построение стратегий до цикла (fail-fast на неизвестные имена)
        _ = self._strategy_cache

    # ── ленивое построение стратегий по парам (имя, ТФ): один раз, доступно с первого тика ──
    @property
    def _strategy_cache(self) -> dict[tuple, object]:
        # Дополняется при обращении: привязка контура на брокере, добавленная на ходу
        # (add-tinvest-orders), приносит новую пару «стратегия + таймфрейм».
        cache = getattr(self, "_strategy_cache_internal", None)
        if cache is None:
            cache = self._strategy_cache_internal = {}
        for items in (
            *self._share_strategies.values(),
            *self._future_strategies.values(),
        ):
            for assignment in items:
                key = (assignment.strategy, assignment.timeframe)
                if key not in cache and assignment.strategy in self._strategy_map:
                    cache[key] = self._strategy_factory(
                        assignment.strategy,
                        config=self._strategy_map[assignment.strategy],
                    )
        return cache

    # ── диагностика ──
    def _maybe_heartbeat(self) -> None:
        if self._heartbeat_every <= 0:
            return
        self._tick_count += 1
        self._heartbeat_countdown -= 1
        if self._heartbeat_countdown > 0:
            return
        self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        lag = getattr(self._data_cache, "feed_lag", None)
        self._bus.publish(
            Event.heartbeat(
                tick_count=self._tick_count,
                error_count=self._errors_in_period,
                feed_lag=lag() if callable(lag) else None,
            )
        )
        self._errors_in_period = 0
        self._heartbeat_countdown = self._heartbeat_every

    def _report_error(self, exc: Exception, operation: str = "обработка тика") -> None:
        self._errors_in_period += 1
        log.exception("Ошибка тика (%s): %s", operation, exc)
        if is_rate_limit(exc):
            return
        self._bus.publish(Event.error(operation=operation))
