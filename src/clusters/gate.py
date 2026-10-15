"""Кластерный вердикт для кандидата нового входа: проверки данных и трасса.

Вызывается из ``TradeManager._plan_entry`` (design D6). Вердикт ``unknown``
никогда не запрещает вход; при ошибке расчёта — тоже ``unknown``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from src.clusters.settings import MODE_OFF, ClusterSettings
from src.clusters.store import ClusterEventStore
from src.clusters.verdict import ALGORITHM, VERSION, ClusterVerdict, evaluate, unknown
from src.instruments.futures import family_for
from src.logging_setup import get_logger
from src.strategies.contracts import SignalType
from src.trade_management.audit import MeasuredValue, TraceLinks, TraceOutcome, calculation_trace

log = get_logger(__name__)
_MONTHS = "FGHJKMNQUVXZ"
_CONTRACT = re.compile(r"^([A-Za-z]+)([FGHJKMNQUVXZ])(\d)$")
CandleSource = Callable[[object], pd.DataFrame]
UidResolver = Callable[[object], str | None]


def card_uid(instrument) -> str | None:
    return getattr(instrument, "uid", None) or None


def store_candles(market_store, source: str, uid_for: UidResolver = card_uid) -> CandleSource:
    """Live: 1m свечи из локального журнала робота (``candle_current``), только завершённые.

    Live-выбор строит инструменты без UID, поэтому адрес пары берётся через
    ``uid_for`` (в роботе — ``MarketDataCache.pair_uid``), а не из карточки.
    """

    def read(instrument) -> pd.DataFrame:
        uid = uid_for(instrument)
        if not uid:
            return pd.DataFrame(columns=["datetime", "high", "low", "close"])
        frame = market_store.current_frame(source=source, instrument_uid=uid, interval="1m")
        if frame.empty:
            return frame
        return frame[frame["is_complete"]].reset_index(drop=True)

    return read


def cache_candles(data_cache) -> CandleSource:
    """История: 1m свечи кэша прогона (тот же базовый поток, что у защиты)."""

    def read(instrument) -> pd.DataFrame:
        data_cache.ensure_loaded(instrument, "1m")
        frame = data_cache.frame_for(instrument, "1m")
        return frame if frame is not None else pd.DataFrame(columns=["datetime", "high", "low", "close"])

    return read


def collector_name(secid: str, robot_short_name: str | None) -> str:
    """Короткое имя контракта коллектора по образцу имени робота: NGV6 + NG-11.26 → NG-10.26."""
    match = _CONTRACT.match(secid)
    prefix = (robot_short_name or "").split("-")[0]
    if not match or not prefix:
        return "другой контракт"
    month = _MONTHS.index(match.group(2)) + 1
    return f"{prefix}-{month:02d}.2{match.group(3)}"


def _family(ticker: str) -> str | None:
    try:
        return family_for(ticker)
    except ValueError:
        return None


class ClusterGate:
    """Считает вердикт по привязке и пишет его трассой ``filter.cluster``."""

    def __init__(self, store: ClusterEventStore, candles: CandleSource, settings: ClusterSettings, *,
                 consumer=None, live: bool = False,
                 clock: Callable[[], datetime] | None = None, uid_for: UidResolver = card_uid) -> None:
        self._store = store
        self._candles = candles
        self._uid_for = uid_for
        self._settings = settings
        self._consumer = consumer
        self._live = live
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def settings(self) -> ClusterSettings:
        return self._settings

    def evaluate(self, assignment, decision, instrument) -> ClusterVerdict | None:
        """``None`` — фильтр у привязки выключен или сигнал не BUY/SELL."""
        mode = getattr(assignment, "clusters", MODE_OFF)
        if mode == MODE_OFF or decision.signal_type not in (SignalType.BUY, SignalType.SELL):
            return None
        if decision.bar_time is None:
            return unknown("candles_missing", "нет времени бара сигнала", mode)
        bar_close = pd.Timestamp(decision.bar_time).to_pydatetime()
        if bar_close.tzinfo is None:
            bar_close = bar_close.replace(tzinfo=timezone.utc)
        try:
            return self._evaluate(mode, decision, instrument, bar_close)
        except Exception as exc:  # вердикт не должен ломать допуск
            log.warning("Кластеры: вердикт %s не посчитан (%s)", instrument.ticker, type(exc).__name__, exc_info=True)
            return unknown("candles_missing", "ошибка расчёта вердикта", mode)

    def _evaluate(self, mode, decision, instrument, bar_close: datetime) -> ClusterVerdict:
        if self._live:
            last = getattr(self._consumer, "last_read_at", None)
            lag = None if last is None else (self._clock() - last).total_seconds()
            if lag is None or lag > self._settings.max_consumer_lag_sec:
                text = "чтение кластеров ещё не началось" if lag is None else f"чтение кластеров отстаёт на {int(lag)} с"
                return unknown("consumer_lag", text, mode)
        ticker = instrument.ticker
        if not self._store.has_events(ticker, until=bar_close):
            return self._uncovered(mode, instrument, bar_close)
        if self._store.last_quality(ticker, until=bar_close) == "data_degraded":
            return unknown("data_degraded", "данные коллектора по инструменту испорчены", mode)
        events = self._store.signals(
            ticker, after=bar_close - timedelta(minutes=self._settings.initiative_window_min), until=bar_close)
        revoked = self._store.revoked((e.signal_id for e in events if e.signal_id), until=bar_close)
        side = 1 if decision.signal_type is SignalType.BUY else -1
        return evaluate(side=side, bar_close=bar_close, candles=self._candles(instrument), events=events,
                        revoked=revoked, settings=self._settings, mode=mode)

    def _uncovered(self, mode, instrument, bar_close: datetime) -> ClusterVerdict:
        if getattr(instrument, "instrument_type", "") == "future":
            family = _family(instrument.ticker)
            others = sorted(secid for secid in self._store.secids(after=bar_close - timedelta(days=1), until=bar_close)
                            if secid != instrument.ticker and family is not None and _family(secid) == family)
            if others:
                theirs = collector_name(others[-1], instrument.short_name)
                ours = instrument.short_name or "контракт не указан"
                return unknown("contract_mismatch", f"коллектор ведёт {theirs}, робот торгует {ours}", mode,
                               collector_secid=others[-1])
        return unknown("no_coverage", "коллектор не ведёт этот инструмент", mode)

    def trace(self, verdict: ClusterVerdict, assignment, decision, instrument, timeframe: str, *, rejected: bool):
        """Трасса ``filter.cluster``: всё, чтобы воспроизвести вердикт и собрать отчёт."""
        inputs = {
            "instrument": MeasuredValue(instrument.ticker, "instrument-ticker"),
            "instrument_uid": MeasuredValue(self._uid_for(instrument) or "", "instrument-id"),
            "short_name": MeasuredValue(getattr(instrument, "short_name", None) or "", "instrument-name"),
            "timeframe": MeasuredValue(timeframe, "timeframe"),
            "side": MeasuredValue(decision.signal_type.value, "signal"),
            "price": MeasuredValue(decision.price, "price"),
            "bar_close": MeasuredValue(pd.Timestamp(decision.bar_time).isoformat(), "utc-time"),
            "strategy": MeasuredValue(getattr(assignment, "strategy", "") or "", "strategy"),
            "mode": MeasuredValue(verdict.mode, "cluster-mode"),
        }
        for key, value in verdict.inputs.items():
            inputs[key] = MeasuredValue(value, "cluster-input")
        return calculation_trace(
            ALGORITHM,
            inputs=inputs,
            result=MeasuredValue(verdict.payload(), "cluster-verdict"),
            reason=verdict.reason,
            formula="1m свечи + события ClusterSource до закрытия бара → вердикт clusters-v1",
            links=TraceLinks(assignment_id=assignment.id, signal_id=decision.event_id),
            outcome=TraceOutcome.REJECTED if rejected else TraceOutcome.ACCEPTED,
            algorithm_version=VERSION,
        )
