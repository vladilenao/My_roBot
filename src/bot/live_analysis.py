"""Live-сценарий: обзор, независимые пары, текущий допуск и диагностика."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4
import pandas as pd

from src.config import TRADE_MANAGEMENT_PROFILES, TRIPLE_SCREEN_PARAMS, TIMEFRAMES, CATCH_UP_BARS
from src.data.live_state import LivePairState, StatusCadence
from src.events.event import Event
from src.history.warmup import binding_requirements
from src.instruments.model import instrument_key
from src.logging_setup import correlation_id_var
from src.strategies.contracts import SignalType


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

    def _calculate(self, instrument, binding, frame):
        tf = binding.timeframe
        frame = frame.tail(self._required(binding)[tf]).reset_index(drop=True).copy()
        strategy = self.bot._strategy_cache[binding.strategy, tf]
        ta = strategy.compute(frame)
        raw = strategy.decide(ta, timeframe=tf)
        close = self.timeline.grid(tf).bar_close(frame["datetime"].iloc[-1])
        raw = replace(raw, bar_time=close, available_at=raw.available_at or close,
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
            self.cache.prepare_live_history(instrument, self.bot._protection_timeframe, 1)
            for tf in sorted({a.timeframe for a in self.bot._strategies_for(instrument)}):
                for binding in self._bindings(instrument, tf):
                    for need_tf, count in self._required(binding).items():
                        self.cache.prepare_live_history(instrument, need_tf, count)

    def bootstrap(self):
        self.register_needs()
        waiter = getattr(self.bot, "_await_live_data", None)
        if callable(waiter):
            waiter()
        for instrument in self.bot._instruments:
            try:
                self.cache.prepare_live_history(instrument, self.bot._protection_timeframe, 1)
            except Exception as exc:
                self.bot._report_error(exc, f"Прогрев {self.bot._display_name(instrument)} 1m")
            for tf in sorted({a.timeframe for a in self.bot._strategies_for(instrument)}):
                bindings = self._bindings(instrument, tf)
                required = max(self._required(a)[tf] for a in bindings)
                rows, frame, reason = [], pd.DataFrame(), ""
                try:
                    frame = self.cache.prepare_live_history(instrument, tf, required)
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

    def expected_close(self, instrument, tf, now):
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

    def publish_status(self, instrument, tf, frame):
        now = self.timeline.now()
        key = self._key(instrument, tf)
        state = self.states[key]
        expected, session, next_open = self.expected_close(instrument, tf, now)
        calendar = self.cache.calendar_for(instrument)
        if state.expected_bar is not None and calendar is not None:
            grid = self.timeline.grid(tf)
            period = pd.Timestamp(grid.next_candle_close(now)) - pd.Timestamp(grid.current_candle_start(now))
            probe = pd.Timestamp(state.expected_bar) - period
            if calendar.covers(probe) and not any(a < state.expected_bar and b > probe for a, b in calendar.intervals):
                state = replace(state, expected_bar=None, publication_deadline=None)
        close = self.timeline.grid(tf).bar_close(frame["datetime"].iloc[-1]) if not frame.empty else None
        state = state.observe(now=now, session=session, expected_bar=expected,
            timeout_secs=self.settings.candle_publication_timeout_secs, received_bar=close, next_open=next_open)
        required = state.required_bars
        reason = "" if len(frame) >= required else f"Недостаточно истории: {len(frame)}/{required} свечей"
        if not reason and not self.cache.coverage_for(instrument, tf, frame.tail(required)):
            reason = "Неразрешённый пробел в диапазоне прогрева"
        state = replace(state, available_bars=len(frame), coverage_complete=not bool(reason), reason=reason)
        if reason:
            state = replace(state, data_state="warming_up" if len(frame) < required else "coverage_blocked")
        elif session == "unknown":
            state = replace(state, reason="Применимое торговое расписание неизвестно")
        error = self.cache._live_errors.get(self.cache._key(instrument, tf))
        if error:
            state = replace(state, reason=error.replace(instrument.ticker, self.bot._display_name(instrument)))
        budget = self.cache._request_budget
        if budget.retry_after is not None and now < budget.retry_after:
            state = replace(state, data_state="rate_limited", reason="Ожидание сброса общего лимита API")
        self.states[key] = state
        if self.cadence.due(state, now):
            self.bot._bus.publish(Event.market_data_status(self.bot._display_name(instrument),
                timeframe=tf, **state.payload(now)))

    def current(self, instrument, tf, close):
        now = pd.Timestamp(self.timeline.now())
        expected, session, _ = self.expected_close(instrument, tf, now)
        if session == "waiting_session":
            return False
        grid = self.timeline.grid(tf)
        period = pd.Timedelta(minutes=1) if tf == "1m" else pd.Timestamp(grid.next_candle_close(now)) - pd.Timestamp(grid.current_candle_start(now))
        if expected is None:
            return False
        # Между close и ожидаемым close считаются только торговые ячейки.
        calendar = self.cache.calendar_for(instrument)
        if pd.Timestamp(expected) - pd.Timestamp(close) > pd.Timedelta(days=7):
            return False
        probe, missed = pd.Timestamp(close), 0
        while probe < expected:
            if calendar is None or not calendar.covers(probe) or calendar.is_open(probe):
                missed += 1
            probe += period
        return missed <= CATCH_UP_BARS

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
                            try:
                                decision, context, _, filtered = self._calculate(instrument, binding, asof)
                                current = self.current(instrument, tf, decision.bar_time) and stamp == frame["datetime"].iloc[-1]
                                # Информационный старый результат не вызывает manage/actions_for_signal.
                                self.bot._publish_decision(decision, instrument, filter_profile=binding.filter_profile,
                                    filtered_out=filtered, timeframe=tf, analysis_mode="live" if current else "historical")
                                if current and execution_complete:
                                    work_frame = asof.tail(self._required(binding)[tf]).reset_index(drop=True).copy()
                                    management_key = instrument_key(instrument), tf, stamp
                                    if management_key not in managed:
                                        self.bot._manage(instrument, bindings, work_frame, context, tf)
                                        managed.add(management_key)
                                    candidates.append(_EntryCandidate(binding, decision, instrument, work_frame, context, tf))
                                self.cursors[cursor_key] = stamp
                                self.cache.mark_analyzed(instrument, tf, binding.id, stamp)
                                state = self.states[self._key(instrument, tf)].with_analysis(decision.bar_time, mode="live" if current else "historical")
                                self.states[self._key(instrument, tf)] = state
                                completed = True
                            except Exception as exc:
                                self.bot._report_error(exc, f"Анализ {self.bot._display_name(instrument)} {tf}")
                                break
            for candidate in sorted(candidates, key=_EntryCandidate.sort_key):
                try:
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
