"""Live-сценарий: обзор, независимые пары, текущий допуск и диагностика."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from uuid import uuid4
import pandas as pd

from src.config import TRADE_MANAGEMENT_PROFILES, TRIPLE_SCREEN_PARAMS, TIMEFRAMES, CATCH_UP_BARS, CLEARING_TIMES
from src.data.live_state import LivePairState, StatusCadence
from src.events.event import Event
from src.history.warmup import binding_requirements
from src.instruments.model import instrument_key
from src.logging_setup import correlation_id_var, get_logger
from src.market_calendar.clearing import clearing_divergence
from src.market_calendar.intervals import exchange_date
from src.strategies.contracts import SignalType

log = get_logger(__name__)


class LiveAnalysis:
    def __init__(self, bot):
        self.bot = bot
        self.cache = bot._data_cache
        self.timeline = bot._timeline
        self.states = {}
        self.cursors = {}
        self.requirements = {}
        self.settings = bot._market_data_settings
        self.cadence = StatusCadence(self.settings.status_repeat_interval_secs)
        self.clearing_times = tuple(CLEARING_TIMES)
        self._clearing_checked = set()

    def _key(self, instrument, tf):
        return instrument_key(instrument), tf

    def _bindings(self, instrument, tf):
        return [a for a in self.bot._strategies_for(instrument) if a.timeframe == tf]

    def _required(self, binding):
        if binding.id not in self.requirements:
            self.requirements[binding.id] = binding_requirements(binding,
                self.bot._strategy_cache[binding.strategy, binding.timeframe],
                TRADE_MANAGEMENT_PROFILES, TRIPLE_SCREEN_PARAMS, tuple(TIMEFRAMES))
        return self.requirements[binding.id]

    def _context(self, instrument, tf, frame):
        if self.bot._context_cache is None:
            return None
        return self.bot._context_cache.context_for_frame(instrument, tf, frame)

    def _warmup_close(self, instrument, tf):
        expectation = self.cache.calendar_expectation(instrument, tf, self.timeline.now())
        return None if expectation is None or expectation.last_due is None else expectation.last_due.close

    def _quality(self, instrument, binding, frame, *, prepare=False):
        requirements = self._required(binding)
        work = requirements[binding.timeframe]
        if len(frame) < work:
            return f"Недостаточно истории: {len(frame)}/{work} свечей"
        if not self.cache.coverage_for(instrument, binding.timeframe, frame.tail(work)):
            return "Неразрешённый пробел в диапазоне прогрева"
        close = self.timeline.grid(binding.timeframe).bar_close(frame["datetime"].iloc[-1])
        for tf, count in requirements.items():
            if tf == binding.timeframe:
                continue
            if prepare:
                higher = self.cache.prepare_live_history(instrument, tf, count, max_close=close)
            else:
                higher = self.cache.frame_for(instrument, tf)
            if not higher.empty:
                higher = higher[higher["datetime"].apply(lambda stamp: self.timeline.grid(tf).bar_close(stamp) <= close)]
            if len(higher) < count:
                return f"Недостаточно истории {tf}: {len(higher)}/{count} свечей"
            if not self.cache.coverage_for(instrument, tf, higher.tail(count)):
                return f"Неполное покрытие прогрева {tf}"
        return ""

    def _input_ready(self, instrument, binding, frame, close):
        """Публикация использованных версий рабочего и старших баров (design 4)."""
        ready_at = getattr(self.cache, "input_ready_at", None)
        if not callable(ready_at):
            return None
        stamps = [ready_at(instrument, binding.timeframe, frame["datetime"].iloc[-1])]
        for tf in self._required(binding):
            if tf in {binding.timeframe, self.bot._protection_timeframe}:
                continue
            higher = self.cache.frame_for(instrument, tf)
            if higher.empty:
                continue
            grid = self.timeline.grid(tf)
            used = higher[higher["datetime"].apply(lambda stamp: grid.bar_close(stamp) <= close)]
            if not used.empty:
                stamps.append(ready_at(instrument, tf, used["datetime"].iloc[-1]))
        stamps = [pd.Timestamp(stamp) for stamp in stamps if stamp is not None]
        return max(stamps) if stamps else None

    def _execution_barrier(self, instrument, close):
        """Критический 1m-префикс UID должен быть доказан до закрытия бара."""
        ready = getattr(self.cache, "execution_ready", None)
        if not callable(ready):
            return ""
        last_minute = pd.Timestamp(close) - pd.Timedelta(minutes=1)
        if ready(instrument, last_minute):
            return ""
        return f"Удержано: базовая 1m не подтверждена до {last_minute:%d.%m.%Y %H:%M} UTC"

    def _ingestion(self, instrument, tf):
        """Технический срез owner для market_data_status (design 11, задача 8.3)."""
        status_of = getattr(self.cache, "pair_status", None)
        if not callable(status_of):
            return None
        status = status_of(instrument, tf)
        transport = status.get("transport") or "polling"
        return {"transport": transport, "epoch": status.get("epoch"),
                "fallback": self.settings.live_transport == "stream" and transport != "stream",
                "data_hold": status.get("state"), "received_through": status.get("received_through"),
                "resolved_through": status.get("resolved_through"), "processed_through": status.get("processed_through"),
                "gap": status.get("gap"), "incident": status.get("incident"),
                "history_error": status.get("history_error") or "",
                "cutover": status.get("cutover"), "pre_cutover_gaps": status.get("pre_cutover_gaps", 0),
                "lanes": status.get("lanes") or {}}

    def _hold_reason(self, instrument, tf, ingestion, close):
        state = ingestion.get("data_hold")
        if state == "INTEGRITY_HOLD":
            incident = ingestion.get("incident") or {}
            return f"Удержано: изменена уже исполненная минута {incident.get('open_time', '')}".strip()
        if state == "CONFLICT_HOLD":
            return "Удержано: расхождение версий свечи, идёт согласующая проверка"
        if state == "GAP_HOLD":
            gap = ingestion.get("gap") or {}
            if gap.get("state") == "parked":
                return (f"Пропуск {gap.get('lower')}–{gap.get('upper')} не восстановлен; "
                        f"следующая проверка {gap.get('next_retry_at')}")
            return f"Восстановление пропуска {gap.get('lower')}–{gap.get('upper')}"
        if state == "RECONCILING":
            return "Согласование потока с историей"
        if state == "BOOTSTRAPPING" and ingestion.get("history_error"):
            return "Прогрев: " + ingestion["history_error"]
        if tf != self.bot._protection_timeframe and close is not None and self.current(instrument, tf, close):
            return self._execution_barrier(instrument, close)
        return ""

    def next_wakeup(self):
        """Ближайший дедлайн публикации или закрытие бара активных пар."""
        now = pd.Timestamp(self.timeline.now())
        moments = [pd.Timestamp(state.publication_deadline) for state in self.states.values()
                   if state.publication_deadline is not None and pd.Timestamp(state.publication_deadline) > now]
        for _, tf in self.states:
            moments.append(pd.Timestamp(self.timeline.grid(tf).next_candle_close(now)))
        return min(moments, default=None)

    def _calculate(self, instrument, binding, frame):
        tf = binding.timeframe
        frame = frame.tail(self._required(binding)[tf]).reset_index(drop=True).copy()
        strategy = self.bot._strategy_cache[binding.strategy, tf]
        ta = strategy.compute(frame)
        raw = strategy.decide(ta, timeframe=tf)
        close = self.timeline.grid(tf).bar_close(frame["datetime"].iloc[-1])
        # Эффективная доступность не раньше закрытия, исходной доступности
        # стратегии и публикации использованного входа; bar_time — закрытие.
        candidates = [pd.Timestamp(item) for item in (raw.available_at, close) if item is not None]
        published = self._input_ready(instrument, binding, frame, close)
        if published is not None:
            candidates.append(published)
        available = max(candidates).to_pydatetime() if candidates else close
        raw = replace(raw, bar_time=close, available_at=available,
            strategy_name=binding.strategy,
            event_id=f"{binding.id}:{instrument_key(instrument)}:{tf}:{pd.Timestamp(close).isoformat()}:{raw.signal_type.value}")
        context = self._context(instrument, tf, frame)
        decision = raw
        if context is not None and self.bot._signal_filter is not None:
            decision = self.bot._signal_filter.apply(raw, context, profile_name=binding.filter_profile,
                instrument=instrument, timeframe=tf)
        filtered = raw.signal_type is not SignalType.HOLD and decision.signal_type is SignalType.HOLD
        return decision, context, ta, filtered

    def register_needs(self):
        """Потребности прогрева без сети: owner передаёт их фоновой полосе."""
        for instrument in self.bot._instruments:
            self.cache.prepare_live_history(instrument, self.bot._protection_timeframe, 1,
                                            max_close=self._warmup_close(instrument, self.bot._protection_timeframe))
            for tf in sorted({a.timeframe for a in self.bot._strategies_for(instrument)}):
                for binding in self._bindings(instrument, tf):
                    for need_tf, count in self._required(binding).items():
                        self.cache.prepare_live_history(instrument, need_tf, count,
                                                        max_close=self._warmup_close(instrument, need_tf))

    def bootstrap(self):
        """Стартовый пакет состояний имеет явную границу для консольной группировки."""
        batch_id = "bootstrap-" + uuid4().hex[:8]
        self.bot._bus.publish(Event.tick_started(tick_id=batch_id))
        try:
            self._bootstrap()
        finally:
            # completed=False запрещает сводку HOLD; накопленные состояния всё же выводятся.
            self.bot._bus.publish(Event.tick_finished(tick_id=batch_id, completed=False))

    def _bootstrap(self):
        self.register_needs()
        waiter = getattr(self.bot, "_await_live_data", None)
        if callable(waiter):
            waiter()
        for instrument in self.bot._instruments:
            try:
                self.cache.prepare_live_history(instrument, self.bot._protection_timeframe, 1,
                                                max_close=self._warmup_close(instrument, self.bot._protection_timeframe))
            except Exception as exc:
                self.bot._report_error(exc, f"Прогрев {self.bot._display_name(instrument)} 1m")
            for tf in sorted({a.timeframe for a in self.bot._strategies_for(instrument)}):
                bindings = self._bindings(instrument, tf)
                required = max(self._required(a)[tf] for a in bindings)
                rows, frame, reason = [], pd.DataFrame(), ""
                try:
                    frame = self.cache.prepare_live_history(instrument, tf, required,
                                                            max_close=self._warmup_close(instrument, tf))
                except Exception as exc:
                    reason = str(exc).replace(instrument.ticker, self.bot._display_name(instrument))
                    frame = self.cache.frame_for(instrument, tf)
                any_calculated = False
                for binding in bindings:
                    row = {"assignment_id": binding.id, "strategy": binding.strategy,
                           "filter_profile": binding.filter_profile, "outcome": "",
                           "indicators": "", "reason": reason}
                    try:
                        quality = self._quality(instrument, binding, frame, prepare=True)
                        row["reason"] = (reason + "; " + quality) if reason and quality else quality
                        if not row["reason"]:
                            decision, _, ta, filtered = self._calculate(instrument, binding, frame)
                            row["outcome"] = "FILTERED" if filtered else decision.signal_type.name
                            row["indicators"] = self.bot._ta_digest(ta)
                            any_calculated = True
                    except Exception as exc:
                        row["reason"] = str(exc).replace(instrument.ticker, self.bot._display_name(instrument))
                    rows.append(row)
                    last = frame["datetime"].iloc[-1] if not frame.empty else None
                    saved = self.cache.analysis_cursor(instrument, tf, binding.id)
                    self.cursors[binding.id, instrument_key(instrument)] = saved if saved is not None else last
                    if saved is None and last is not None and row["outcome"]:
                        self.cache.mark_analyzed(instrument, tf, binding.id, last)
                close = self.timeline.grid(tf).bar_close(frame["datetime"].iloc[-1]) if not frame.empty else None
                complete = any_calculated and not any(row["reason"] for row in rows)
                state = LivePairState("tbank_exchange", instrument_key(instrument), tf,
                    received_bar=close, analyzed_bar=close if any_calculated else None,
                    analysis_mode="overview" if any_calculated else "", available_bars=len(frame),
                    required_bars=required, coverage_complete=complete,
                    reason=next((row["reason"] for row in rows if row["reason"]), ""))
                self.states[self._key(instrument, tf)] = state
                self.bot._bus.publish(Event.startup_analysis(self.bot._display_name(instrument),
                    published_at=self.timeline.now(), bar_time=close, timeframe=tf, results=rows,
                    available_bars=len(frame), required_bars=required, coverage_complete=complete, reason=state.reason))
                self.publish_status(instrument, tf, frame)

    def publish_status(self, instrument, tf, frame):
        now = self.timeline.now()
        key = self._key(instrument, tf)
        state = self.states[key]
        expectation = self.cache.calendar_expectation(instrument, tf, now)
        if expectation is None or expectation.phase.value == "unknown":
            # UNKNOWN не подавляет задержку: прежняя сеточная семантика.
            expected, session, next_open = self._legacy_expectation(instrument, tf, now)
            phase, due = "unknown", None
        else:
            phase = expectation.phase.value
            session = "open" if phase == "trading" else ("waiting_session" if phase in {"break", "closed"} else "unknown")
            due = expectation.last_due
            expected = None if due is None else due.close
            next_open = None if expectation.next_event is None or expectation.next_event.kind != "session_open" else expectation.next_event.at
        close = self.timeline.grid(tf).bar_close(frame["datetime"].iloc[-1]) if not frame.empty else None
        ingestion = self._ingestion(instrument, tf)
        resolved = ingestion.get("resolved_through") if ingestion else None
        resolved_close = (self.timeline.grid(tf).bar_close(pd.Timestamp(resolved).tz_convert("UTC").tz_localize(None))
                          if resolved else None)
        state = state.observe(now=now, session=session, expected_bar=expected,
            timeout_secs=self.settings.candle_publication_timeout_secs, received_bar=close, next_open=next_open,
            resolved_bar=resolved_close, publication_deadline=None if due is None else due.publication_deadline)
        state = replace(state, phase=phase, break_kind=None if expectation is None else expectation.break_kind,
                        last_due=due, next_event=None if expectation is None else expectation.next_event,
                        schedule=None if expectation is None else expectation.schedule)
        required = state.required_bars
        reason = "" if len(frame) >= required else f"Недостаточно истории: {len(frame)}/{required} свечей"
        if not reason and not self.cache.coverage_for(instrument, tf, frame.tail(required)):
            reason = "Неразрешённый пробел в диапазоне прогрева"
        unknown_schedule = "Применимое торговое расписание неизвестно"
        state = replace(state, available_bars=len(frame), coverage_complete=not bool(reason), reason=reason)
        if reason:
            state = replace(state, data_state="warming_up" if len(frame) < required else "coverage_blocked")
        elif session == "unknown":
            state = replace(state, reason=unknown_schedule)
        error = self.cache._live_errors.get(self.cache._key(instrument, tf))
        if error:
            state = replace(state, reason=error.replace(instrument.ticker, self.bot._display_name(instrument)))
        if ingestion and state.reason in {"", unknown_schedule}:
            hold = self._hold_reason(instrument, tf, ingestion, close)
            if hold:
                # Удержание блокирует торговлю и важнее общей пометки о
                # расписании; причины прогрева/покрытия остаются первыми.
                state = replace(state, reason=hold + ("; расписание неизвестно" if state.reason == unknown_schedule else ""))
        state = replace(state, ingestion=ingestion)
        budget = self.cache._request_budget
        if budget.retry_after is not None and now < budget.retry_after:
            state = replace(state, data_state="rate_limited", reason="Ожидание сброса общего лимита API")
        self.states[key] = state
        if self.cadence.due(state, now):
            self.bot._bus.publish(Event.market_data_status(self.bot._display_name(instrument),
                timeframe=tf, **state.payload(now)))

    def current(self, instrument, tf, close):
        now = pd.Timestamp(self.timeline.now())
        expectation = self.cache.calendar_expectation(instrument, tf, now)
        if expectation is None or expectation.phase.value == "unknown":
            expected, session, _ = self._legacy_expectation(instrument, tf, now)
            if session == "waiting_session" or expected is None:
                return False
            grid = self.timeline.grid(tf)
            period = pd.Timedelta(minutes=1) if tf == "1m" else pd.Timestamp(grid.next_candle_close(now)) - pd.Timestamp(grid.current_candle_start(now))
            if pd.Timestamp(expected) - pd.Timestamp(close) > pd.Timedelta(days=7):
                return False
            calendar, probe, missed = self.cache.calendar_for(instrument), pd.Timestamp(close), 0
            while probe < pd.Timestamp(expected):
                if calendar is None or not calendar.covers(probe) or calendar.is_open(probe):
                    missed += 1
                probe += period
            return missed <= CATCH_UP_BARS
        if expectation.phase.value != "trading" or expectation.last_due is None:
            return False
        expected = pd.Timestamp(expectation.last_due.close)
        candidate = pd.Timestamp(close)
        if candidate > expected:
            return False
        # Пропущенные закрытые бары: открытие от close анализа до открытия
        # last_due включительно; текущий незакрытый бар не считается.
        due = self.cache.expected_calendar_bars(instrument, tf, candidate, expectation.last_due.open)
        return len(due) <= CATCH_UP_BARS

    def _legacy_expectation(self, instrument, tf, now):
        """Переходная адаптация тестовых/старых SessionCalendar без расписания."""
        grid = self.timeline.grid(tf)
        calendar = self.cache.calendar_for(instrument)
        if calendar is None or not calendar.covers(now):
            return grid.bar_close(grid.previous_candle_start(now)), "unknown", None
        ends = []
        for lower, upper in calendar.intervals:
            if lower > now:
                continue
            start = min(pd.Timestamp(grid.previous_candle_start(now)),
                        pd.Timestamp(grid.current_candle_start(upper - pd.Timedelta(microseconds=1))))
            close = grid.bar_close(start)
            if close <= now and close > lower:
                ends.append(close)
        return max(ends, default=None), "open" if calendar.is_open(now) else "waiting_session", calendar.next_open(now)

    def diagnose_clearing(self, now=None):
        """Расхождение клиринга расписания и конфига — раз на режим, дату и версию.

        Поведение ``JournalBroker`` не меняется; это только предупреждение.
        """
        calendar = getattr(self.cache, "market_calendar", None)
        if calendar is None or not hasattr(calendar, "exchange_for"):
            return []
        now = pd.Timestamp(now or self.timeline.now()).to_pydatetime()
        today, messages = exchange_date(now), []
        for instrument in self.bot._instruments:
            if getattr(instrument, "instrument_type", None) != "future":
                continue  # clearing_times — клиринг срочного рынка; у акций окон клиринга нет
            uid = self.cache.instrument_uid(instrument, "1m")
            exchange = calendar.exchange_for(uid) if uid else None
            trading = None if exchange is None else calendar.calendar(exchange, now)
            day = None if trading is None else trading.day(today)
            if day is None or (exchange, day.trading_date, day.version) in self._clearing_checked:
                continue
            self._clearing_checked.add((exchange, day.trading_date, day.version))
            divergence = clearing_divergence(day, self.clearing_times)
            if divergence:
                message = f"Клиринг {exchange} на {day.trading_date}: {divergence}. Клиринг идёт по конфигу."
                log.warning(message)
                messages.append(message)
        return messages

    def step(self):
        from src.bot.trading_bot import _EntryCandidate
        tick_id = uuid4().hex[:8]
        correlation_id_var.set(tick_id)
        self.bot._bus.publish(Event.tick_started(tick_id=tick_id))
        completed = False
        candidates = []
        managed = set()
        try:
            self.cache.refresh_live()
            self.diagnose_clearing()
            execution_complete = self.bot._post_tick({self.bot._protection_timeframe}) is not False if self.bot._post_tick else True
            for instrument in self.bot._instruments:
                for tf in sorted({a.timeframe for a in self.bot._strategies_for(instrument)}):
                    frame = self.cache.frame_for(instrument, tf)
                    bindings = self._bindings(instrument, tf)
                    self.publish_status(instrument, tf, frame)
                    for binding in bindings:
                        cursor_key = binding.id, instrument_key(instrument)
                        cursor = self.cursors.get(cursor_key)
                        fresh = frame if cursor is None else frame[frame["datetime"] > cursor]
                        for stamp in fresh["datetime"] if not fresh.empty else ():
                            asof = frame[frame["datetime"] <= stamp].copy()
                            reason = self._quality(instrument, binding, asof)
                            if reason:
                                break
                            close = self.timeline.grid(tf).bar_close(stamp)
                            if stamp == frame["datetime"].iloc[-1] and self.current(instrument, tf, close):
                                barrier = self._execution_barrier(instrument, close)
                                if barrier:
                                    # Курсор не двигается: решение дождётся
                                    # непрерывной базовой 1m, а не потеряется.
                                    key = self._key(instrument, tf)
                                    self.states[key] = replace(self.states[key], reason=barrier)
                                    break
                            publish = None
                            try:
                                decision, context, _, filtered = self._calculate(instrument, binding, asof)
                                current = self.current(instrument, tf, decision.bar_time) and stamp == frame["datetime"].iloc[-1]
                                publish = partial(self.bot._publish_decision, decision, instrument,
                                    filter_profile=binding.filter_profile, filtered_out=filtered, timeframe=tf,
                                    analysis_mode="live" if current else "historical")
                                # Информационный старый результат не вызывает manage/actions_for_signal.
                                if current and execution_complete:
                                    work_frame = asof.tail(self._required(binding)[tf]).reset_index(drop=True).copy()
                                    management_key = instrument_key(instrument), tf, stamp
                                    if management_key not in managed:
                                        self.bot._manage(instrument, bindings, work_frame, context, tf)
                                        managed.add(management_key)
                                    # Решение публикуется перед своим допуском: ПЛАН/ОТКЛОНЕНО
                                    # идут в консоли одним блоком с СИГНАЛ.
                                    candidates.append((_EntryCandidate(binding, decision, instrument, work_frame, context, tf), publish))
                                    publish = None
                                else:
                                    pending, publish = publish, None
                                    pending()
                                self.cursors[cursor_key] = stamp
                                self.cache.mark_analyzed(instrument, tf, binding.id, stamp)
                                state = self.states[self._key(instrument, tf)].with_analysis(decision.bar_time, mode="live" if current else "historical")
                                self.states[self._key(instrument, tf)] = state
                                completed = True
                            except Exception as exc:
                                if publish is not None:
                                    publish()
                                self.bot._report_error(exc, f"Анализ {self.bot._display_name(instrument)} {tf}")
                                break
            for candidate, publish in sorted(candidates, key=lambda item: item[0].sort_key()):
                try:
                    publish()
                    self.bot._admit_candidate(candidate)
                except Exception as exc:
                    self.bot._report_error(exc, f"Допуск {self.bot._display_name(candidate.instrument)} {candidate.timeframe}")
        except BaseException:
            completed = False
            raise
        finally:
            self.bot._maybe_heartbeat()
            self.bot._bus.publish(Event.tick_finished(tick_id=tick_id, completed=completed))
            correlation_id_var.set(None)
