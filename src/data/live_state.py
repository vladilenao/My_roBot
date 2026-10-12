"""Независимые состояния live-пары и ограниченная публикация диагностики."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import isfinite
from numbers import Integral

import numpy as np

from src.data.timeutil import to_utc_naive


@dataclass(frozen=True)
class StreamClosureEvidence:
    """Доказательство конкретного acknowledged режима, не финальности OHLC."""

    uid: str
    timeframe: str
    epoch: int
    status: int
    action: int
    waiting_close: bool
    requested_source: int = 0
    echoed_source: int | None = None

    def permits(self, uid, timeframe, epoch):
        return (self.uid == uid and self.timeframe == timeframe and self.epoch == epoch
                and self.status == 1 and self.action == 1 and self.waiting_close is True
                and self.requested_source == 0 and self.echoed_source in (None, 0))


@dataclass(frozen=True)
class CandleObservation:
    """Неизменяемый транспортный вход единственного владельца свечи."""

    uid: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    received_at: datetime
    prices: tuple[float, float, float, float]
    volume: int
    closed: bool
    transport: str
    closure_evidence: str
    actual_source: int | None
    requested_source: int = 0
    epoch: int = 0
    last_trade_time: datetime | None = None

    def as_row(self):
        return dict(zip(("open", "high", "low", "close"), self.prices),
                    datetime=self.open_time, volume=self.volume, is_complete=self.closed)


def _observation(uid, timeframe, opening, received_at, prices, volume, *, closed, transport,
                 closure_evidence, actual_source, epoch=0, last_trade_time=None):
    from src.scheduler.timing import CandleScheduler
    if not isinstance(uid, str) or not uid:
        raise ValueError("Рыночное наблюдение требует UID")
    opening, received = to_utc_naive(opening), to_utc_naive(received_at)
    if opening is None or received is None or np.isnat(opening.to_datetime64()) or np.isnat(received.to_datetime64()):
        raise ValueError("Рыночное наблюдение требует определённое UTC-время")
    grid = CandleScheduler(timeframe)
    if grid.current_candle_start(opening) != opening:
        raise ValueError("Открытие свечи не совпадает с нативной UTC-сеткой")
    values = tuple(float(value) for value in prices)
    if len(values) != 4 or not all(isfinite(value) for value in values):
        raise ValueError("Рыночное наблюдение требует конечные OHLC")
    if isinstance(volume, bool) or not isinstance(volume, Integral) or volume < 0:
        raise ValueError("Рыночное наблюдение требует неотрицательный целый объём")
    if actual_source not in (None, 0, 1, 2):
        raise ValueError("Несовместимый фактический источник свечи")
    close = grid.bar_close(opening)
    closed = bool(closed and close <= received)
    return CandleObservation(uid, timeframe, opening, close, received, values, int(volume), closed,
        transport, closure_evidence if closed else "not_closed", actual_source,
        epoch=epoch, last_trade_time=None if last_trade_time is None else to_utc_naive(last_trade_time))


def normalize_stream_candle(candle, *, evidence: StreamClosureEvidence | None, epoch, received_at):
    """Stream не имеет is_complete: ACK режима и grid проверяются явно."""
    if candle.timeframe is None:
        raise ValueError("Неподдержанный нативный stream-интервал")
    closed = evidence is not None and evidence.permits(candle.uid, candle.timeframe, epoch)
    return _observation(candle.uid, candle.timeframe, candle.open_time, received_at,
        candle.prices, candle.volume, closed=closed, transport="stream",
        closure_evidence="ack_waiting_close_grid", actual_source=candle.source,
        epoch=epoch, last_trade_time=candle.last_trade_time)


def normalize_rpc_candle(row, *, uid, timeframe, received_at, actual_source=None):
    """Завершённость RPC никогда не угадывается по отсутствующему полю."""
    flag = row.get("is_complete")
    closed = isinstance(flag, (bool, np.bool_)) and bool(flag)
    return _observation(uid, timeframe, row["datetime"], received_at,
        tuple(row[key] for key in ("open", "high", "low", "close")), row["volume"],
        closed=closed, transport="rpc", closure_evidence="rpc_is_complete",
        actual_source=actual_source)


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
