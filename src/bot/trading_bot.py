from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from src.instruments import Instrument, normalize_instrument
from src.logging_setup import correlation_id_var, get_logger
from src.notifier.errors import user_error_message
from src.strategies.contracts import Assignment, SignalType
from src.strategies.registry import get_strategy, validate_assignments
from src.trade_management.actions import AddToTrade, TradeAction

log = get_logger(__name__)


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
            self.instrument.ticker,
            self.decision.event_id or "",
        )


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
        notifier,
        strategy_map: dict[str, object],
        data_cache,
        timeline,
        execution,
        strategy_factory=get_strategy,
        share_strategies: dict[str, list[Assignment]] | None = None,
        future_strategies: dict[str, list[Assignment]] | None = None,
        heartbeat_every_ticks: int | None = 60,
        tick_poll_secs: float = 1.0,
        tick_timeout_secs: float = 65.0,
        context_cache=None,
        signal_filter=None,
        risk_manager=None,
        post_tick=None,
        trade_manager=None,
        action_executor=None,
        protection_timeframe: str = "1m",
    ) -> None:
        self._notifier = notifier
        self._strategy_map = strategy_map
        self._data_cache = data_cache
        self._timeline = timeline
        self._execution = execution
        self._strategy_factory = strategy_factory
        self._share_strategies = share_strategies or {}
        self._future_strategies = future_strategies or {}
        self._heartbeat_every = heartbeat_every_ticks or 0
        self._tick_poll_secs = tick_poll_secs
        self._tick_timeout_secs = tick_timeout_secs
        self._context_cache = context_cache
        self._signal_filter = signal_filter
        self._risk_manager = risk_manager
        self._post_tick = post_tick
        self._trade_manager = trade_manager
        self._action_executor = action_executor
        self._protection_timeframe = protection_timeframe

        self._instruments = [
            i if isinstance(i, Instrument) else normalize_instrument(i)
            for i in instruments
        ]
        self._tick_count = 0
        self._errors_in_period = 0
        self._heartbeat_countdown = self._heartbeat_every

    # ── ПУНКТ 1: запуск ──
    def run(self) -> None:
        self._validate()
        log.info("Робот запущен. Ctrl+C для остановки.")
        self._loop()

    # ── ПУНКТ 2: бесконечный цикл «тик = закрытая свеча активного ТФ» ──
    def _loop(self) -> None:
        first = True
        while True:
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
            except Exception as exc:
                self._report_error(exc, "обработка тика")
                time.sleep(self._timeline.fallback_secs())

    # ── ПУНКТ 2.0: первый тик при запуске без ожидания границы ──
    def _bootstrap(self) -> set[str]:
        """Первичное наполнение кадров и ограниченное ожидание свежего закрытого бара.

        Не дожидается ближайшей календарной границы: сразу анализирует последнюю
        уже закрытую свечу каждого активного ТФ. Если свежий закрытый бар ещё не
        опубликован (запуск на границе), ждёт его появления с тем же таймаутом,
        что и штатный тик. Возвращает ТФ с готовой свежей свечой.
        """
        for instrument in self._instruments:
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
        correlation_id_var.set(uuid4().hex[:8])
        try:
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
            if not ready_tfs:
                return
            # Protection is simulated from closed base bars before any strategy
            # may calculate a new entry from a slower timeframe.
            if self._protection_timeframe in ready_tfs and self._post_tick is not None:
                self._post_tick({self._protection_timeframe})
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
                    self._admit_candidate(candidate)
                except _OperationError as err:
                    self._report_error(err.cause, err.operation)
                except Exception as exc:
                    self._report_error(exc, f"портфельный допуск {candidate.instrument.label}")
            self._maybe_heartbeat()
        finally:
            correlation_id_var.set(None)

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
        management_tfs = {self._protection_timeframe} if self._trade_manager is not None else set()
        for tf in sorted(
            (assigned_tfs | management_tfs) & ready_tfs,
            key=lambda timeframe: (timeframe != self._protection_timeframe, timeframe),
        ):
            frame = self._data_cache.frame_for(instrument, tf)
            if frame.empty:
                log.info("Нет готовых свечей для %s (%s) — пропускаем.", self._display_name(instrument), tf)
                continue
            context = self._context_cache.get_context(instrument, tf) if self._context_cache else None
            tf_assignments = [a for a in assignments if a.timeframe == tf]
            log.debug(
                "%s %s | получено свечей=%d, последний бар O=%.4f H=%.4f L=%.4f C=%.4f",
                self._display_name(instrument), tf, len(frame),
                frame["open"].iloc[-1], frame["high"].iloc[-1],
                frame["low"].iloc[-1], frame["close"].iloc[-1],
            )
            if context is not None:
                log.debug("%s %s | контекст: %s", self._display_name(instrument), tf, context)
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
                log.debug(
                    "%s %s | %s → %s %s",
                    self._display_name(instrument), tf, name,
                    decision.signal_type.name,
                    self._ta_digest(ta),
                )
                bar_time = self._timeline.grid(tf).bar_close(frame["datetime"].iloc[-1])
                decision = replace(
                    decision,
                    bar_time=bar_time,
                    available_at=decision.available_at or bar_time,
                    event_id=decision.event_id or f"{assignment.id}:{instrument.ticker}:{tf}:{bar_time.isoformat()}:{decision.signal_type.value}",
                )
                if self._trade_manager is not None:
                    actions_for_signal = getattr(self._trade_manager, "actions_for_signal", None)
                    if actions_for_signal is not None:
                        candidate = _EntryCandidate(
                            assignment, decision, instrument, frame, context, tf,
                        )
                        if entry_candidates is None:
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
        self._dispatch_management_actions(
            actions_for_signal(
                candidate.assignment,
                candidate.decision,
                candidate.instrument,
                candidate.frame,
                candidate.context,
                timeframe=candidate.timeframe,
            ),
            candidate.decision,
            candidate.context,
            candidate.assignment,
            candidate.instrument,
            candidate.timeframe,
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
            executor = self._action_executor or self._execution
            submit = getattr(executor, "submit", None)
            if submit is None:
                raise TypeError("action executor must support submit(action, now)")
            submit(action, datetime.now(timezone.utc).replace(microsecond=0))

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
        return instrument.short_name or instrument.ticker

    # ── ПУНКТ 4.2.3-4.2.4: доставка через порт на каждом тике ──
    def _emit(self, instrument: Instrument, name: str, decision, *, filter_profile: str = "", filtered_out: bool = False, timeframe: str = "") -> None:
        self._execution.execute(
            decision, instrument,
            filter_profile=filter_profile, filtered_out=filtered_out,
            timeframe=timeframe,
        )

    # ── диспетчеризация привязок по типу инструмента ──
    def _strategies_for(self, instrument: Instrument) -> list[Assignment]:
        if instrument.instrument_type == "future":
            return self._future_strategies.get(instrument.ticker[:2].upper(), [])
        return self._share_strategies.get(instrument.ticker, [])

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
        cache = getattr(self, "_strategy_cache_internal", None)
        if cache is None:
            cache = {}
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
            self._strategy_cache_internal = cache
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
        message = (
            f"💓 Сердцебиение: тиков работы — {self._tick_count}, "
            f"ошибок за период — {self._errors_in_period}."
        )
        try:
            self._notifier.notify(message)
        except Exception:
            log.exception("Не удалось доставить сердцебиение.")
        self._errors_in_period = 0
        self._heartbeat_countdown = self._heartbeat_every

    def _report_error(self, exc: Exception, operation: str = "обработка тика") -> None:
        self._errors_in_period += 1
        log.exception("Ошибка тика (%s): %s", operation, exc)
        message = user_error_message(exc, operation)
        if message is None:
            return
        try:
            self._notifier.notify(message)
        except Exception:
            log.exception("Не удалось уведомить об ошибке робота.")
