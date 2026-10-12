"""Независимые состояния live-пары и ограниченная публикация диагностики."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta


@dataclass(frozen=True)
class LivePairState:
    source: str
    instrument_uid: str
    timeframe: str
    session: str = "unknown"
    data_state: str = "warming_up"
    received_bar: datetime | None = None
    analyzed_bar: datetime | None = None
    analysis_mode: str = ""
    expected_bar: datetime | None = None
    publication_deadline: datetime | None = None
    next_open: datetime | None = None
    available_bars: int = 0
    required_bars: int = 0
    coverage_complete: bool = False
    reason: str = ""

    def observe(self, *, now: datetime, session: str, expected_bar: datetime | None,
                timeout_secs: float, received_bar: datetime | None = None,
                next_open: datetime | None = None) -> "LivePairState":
        """Дедлайн привязан к первому неразрешённому закрытию, а не к опросу.

        Все времена — UTC; expected_bar/received_bar означают закрытие свечи.
        Календарный перерыв не отменяет незавершённое ожидание конца сессии.
        """
        received = received_bar if received_bar is not None else self.received_bar
        expected, deadline = self.expected_bar, self.publication_deadline
        if expected is not None and received is not None and received >= expected:
            expected, deadline = None, None
        if expected is None and expected_bar is not None and (received is None or received < expected_bar):
            expected = expected_bar
            deadline = expected + timedelta(seconds=timeout_secs)
        state = "current" if received is not None else "warming_up"
        if expected is not None:
            state = "delayed" if deadline is not None and now >= deadline else "waiting_bar"
        elif session == "waiting_session":
            state = "waiting_bar"
        return replace(self, session=session, data_state=state, received_bar=received,
                       expected_bar=expected, publication_deadline=deadline, next_open=next_open)

    def with_analysis(self, bar_time: datetime, *, mode: str) -> "LivePairState":
        return replace(self, analyzed_bar=bar_time, analysis_mode=mode)

    def payload(self, observed_at: datetime) -> dict:
        return {"session": self.session, "data_state": self.data_state,
                "observed_at": observed_at, "reason": self.reason,
                "received_bar": self.received_bar, "analyzed_bar": self.analyzed_bar,
                "analysis_mode": self.analysis_mode, "expected_bar": self.expected_bar,
                "publication_deadline": self.publication_deadline, "next_open": self.next_open,
                "available_bars": self.available_bars, "required_bars": self.required_bars,
                "coverage_complete": self.coverage_complete}


class StatusCadence:
    """Переходы сразу; одинаковое ожидание не чаще заданного интервала."""

    def __init__(self, repeat_seconds: float):
        self._repeat = timedelta(seconds=repeat_seconds)
        self._last: dict[tuple, tuple[tuple, datetime]] = {}

    def due(self, state: LivePairState, now: datetime) -> bool:
        key = (state.source, state.instrument_uid, state.timeframe)
        signature = (state.session, state.data_state, state.reason, state.coverage_complete)
        previous = self._last.get(key)
        if previous is not None and previous[0] == signature and now - previous[1] < self._repeat:
            return False
        self._last[key] = (signature, now)
        return True
