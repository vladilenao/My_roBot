"""Вердикт кластерного фильтра ``clusters-v1`` (design D5).

Чистая функция от 1m свечей, событий ClusterSource и порогов: одинаково
считается в live и в истории. Время — aware UTC; свечи — кадр с наивным UTC
``datetime`` (время открытия), ``high``, ``low``, ``close``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from src.clusters.settings import MODE_ENFORCE, MODE_SHADOW, ClusterSettings
from src.clusters.store import StoredEvent

ALGORITHM = "filter.cluster"
VERSION = "clusters-v1"
MSK = timedelta(hours=3)
MINUTE = timedelta(minutes=1)
PRE_WINDOW = timedelta(minutes=15)
RANGE_WINDOW = timedelta(minutes=120)
# Последняя 1m свеча старше этого — цена на входе неизвестна.
STALE_CANDLE = timedelta(minutes=15)

VETO_STRONG, VETO, CONFIRM, NEUTRAL, UNKNOWN = "veto_strong", "veto", "confirm", "neutral", "unknown"
INITIATIVE_TYPES = frozenset({"large_aggressor", "stacked_imbalance"})


@dataclass(frozen=True)
class Evidence:
    """Учтённое событие ClusterSource и его метка по ходу цены до него."""

    event_id: str
    event_type: str
    direction: str
    relation: str  # same / opposite
    published_at: datetime
    own: float | None
    label: str  # climax / initiative / absorption_against / other
    counts_for: str  # veto / confirm / none
    payload: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self, bar_close: datetime) -> dict[str, object]:
        return {
            "event_id": self.event_id, "event_type": self.event_type, "direction": self.direction,
            "relation": self.relation, "published_at": self.published_at.isoformat(),
            "age_min": round((bar_close - self.published_at).total_seconds() / 60, 1),
            "own_pre_move_sigma": None if self.own is None else round(self.own, 4),
            "label": self.label, "counts_for": self.counts_for,
        }


@dataclass(frozen=True)
class ClusterVerdict:
    """Итог для одного сигнала; ``inputs`` — то, что пишется в трассу."""

    verdict: str
    reason: str
    message: str
    mode: str
    inputs: Mapping[str, object] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()

    @property
    def vetoes(self) -> bool:
        return self.verdict in (VETO, VETO_STRONG)

    def blocks(self, enforce_level: str) -> bool:
        """Даёт ли вердикт недопуск в режиме enforce."""
        if self.mode != MODE_ENFORCE:
            return False
        return self.verdict == VETO_STRONG or (self.verdict == VETO and enforce_level == VETO)

    def payload(self) -> dict[str, str]:
        """Ключ ``clusters`` событий ``signal``/``rejected``."""
        return {"verdict": self.verdict, "reason": self.reason, "message": self.message}


def unknown(reason: str, message: str, mode: str, **inputs: object) -> ClusterVerdict:
    return ClusterVerdict(UNKNOWN, reason, "нет данных — " + message, mode, dict(inputs))


def _naive(value: datetime) -> pd.Timestamp:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return pd.Timestamp(value)


def _msk_day(open_time: pd.Series) -> pd.Series:
    return (open_time + MSK).dt.date


class _Closes:
    """``close_at(t)`` — закрытие последней 1m свечи, закрывшейся не позже ``t``."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._close_times = (frame["datetime"] + MINUTE).to_numpy(dtype="datetime64[ns]")
        self._open = frame["datetime"].to_numpy(dtype="datetime64[ns]")
        self._closes = frame["close"].to_numpy(dtype=float)

    def at(self, moment: datetime) -> tuple[pd.Timestamp, float] | None:
        bound = np.datetime64(_naive(moment).to_datetime64())
        index = int(np.searchsorted(self._close_times, bound, side="right")) - 1
        if index < 0:
            return None
        return pd.Timestamp(self._open[index]), float(self._closes[index])


def sigma15(frame: pd.DataFrame, day: date, days: int) -> tuple[float | None, int, int]:
    """Медиана |ln(close_t / close_{t−15м})| внутри дня за ``days`` торговых дней до ``day``.

    Возвращает (σ15 как доля, число дней, число значений).
    """
    if frame.empty:
        return None, 0, 0
    msk = _msk_day(frame["datetime"])
    history_days = sorted(d for d in set(msk) if d < day)[-days:]
    if len(history_days) < days:
        return None, len(history_days), 0
    hist = frame[msk.isin(history_days)]
    closes = pd.Series(hist["close"].to_numpy(dtype=float), index=pd.DatetimeIndex(hist["datetime"]))
    closes = closes[~closes.index.duplicated(keep="last")]
    earlier = closes.reindex(closes.index - PRE_WINDOW).to_numpy()
    same_day = (closes.index - PRE_WINDOW + MSK).date == (closes.index + MSK).date
    valid = same_day & ~np.isnan(earlier) & (earlier > 0)
    moves = np.abs(np.log(closes.to_numpy()[valid] / earlier[valid]))
    if not len(moves):
        return None, len(history_days), 0
    value = float(np.median(moves))
    return (value if value > 0 else None), len(history_days), int(len(moves))


def _ru(value: float, places: int = 2, *, signed: bool = False) -> str:
    text = f"{value:+.{places}f}" if signed else f"{value:.{places}f}"
    return text.replace(".", ",").replace("-", "−")


def _trim(value: float) -> str:
    return (f"{value:.2f}".rstrip("0").rstrip(".")).replace(".", ",")


def _kind(event: StoredEvent | Evidence) -> str:
    """Вид события по-русски: «всплеск продаж ×9,6», «крупная покупка 1 300 контр.»."""
    payload = event.payload
    buying = event.direction == "long"
    if event.event_type == "volume_spike":
        ratio = payload.get("ratio")
        text = "всплеск " + ("покупок" if buying else "продаж")
        return text + (f" ×{_ru(float(ratio), 1)}" if isinstance(ratio, (int, float)) else "")
    if event.event_type == "large_aggressor":
        text = "крупная " + ("покупка" if payload.get("side", "BUY" if buying else "SELL") == "BUY" else "продажа")
        size = payload.get("size")
        if isinstance(size, (int, float)):
            if payload.get("size_unit") == "rub":
                text += f" {_ru(float(size) / 1e6, 1)} млн ₽"
            else:
                text += f" {int(size):,} контр.".replace(",", " ")
        return text
    if event.event_type == "stacked_imbalance":
        return "лестница " + ("покупок" if payload.get("side", "BUY" if buying else "SELL") == "BUY" else "продаж")
    if event.event_type == "absorption":
        return "поглощение " + ("покупок" if payload.get("absorbed_side") == "BUY" else "продаж")
    return event.event_type


def _clock(moment: datetime) -> str:
    return (moment.astimezone(timezone.utc) + MSK).strftime("%H:%M")


def evaluate(
    *,
    side: int,
    bar_close: datetime,
    candles: pd.DataFrame,
    events: Iterable[StoredEvent],
    revoked: set[str],
    settings: ClusterSettings,
    mode: str,
) -> ClusterVerdict:
    """Шаги 1–7 алгоритма; проверки данных шага 0, кроме σ и свечей, — у вызывающего."""
    t = bar_close.astimezone(timezone.utc)
    frame = candles[["datetime", "high", "low", "close"]].copy() if not candles.empty else candles
    if not frame.empty:
        frame = frame.sort_values("datetime")
        frame = frame[frame["datetime"] + MINUTE <= _naive(t)].reset_index(drop=True)
    day = (t + MSK).date()
    sigma, sigma_days_found, sigma_n = sigma15(frame, day, settings.sigma_days)
    if sigma is None:
        return unknown("no_sigma_history",
                       f"мало 1m истории для σ ({sigma_days_found} из {settings.sigma_days} дней)",
                       mode, sigma_days=sigma_days_found)
    closes = _Closes(frame)
    at_entry = closes.at(t)
    before = closes.at(t - PRE_WINDOW)
    if at_entry is None or before is None or _naive(t) - (at_entry[0] + MINUTE) > STALE_CANDLE:
        return unknown("candles_missing", "нет 1m свечей к моменту сигнала", mode)
    pre = side * math.log(at_entry[1] / before[1]) / sigma
    window = frame[(frame["datetime"] >= _naive(t - RANGE_WINDOW)) & (frame["datetime"] <= _naive(t - MINUTE))]
    high = float(window["high"].max()) if not window.empty else at_entry[1]
    low = float(window["low"].min()) if not window.empty else at_entry[1]
    position = 0.5 if high == low else (at_entry[1] - low) / (high - low)
    position_side = position if side > 0 else 1 - position
    entry_side = "BUY" if side > 0 else "SELL"

    evidence: list[Evidence] = []
    initiative_from = t - timedelta(minutes=settings.initiative_window_min)
    climax_from = t - timedelta(minutes=settings.climax_window_min)
    for event in events:
        if event.published_at <= initiative_from or event.published_at > t or event.signal_id in revoked:
            continue
        direction = {"long": 1, "short": -1}.get(event.direction or "")
        if direction is None:
            continue
        relation = "same" if direction == side else "opposite"
        now_close, earlier = closes.at(event.published_at), closes.at(event.published_at - PRE_WINDOW)
        own = None if now_close is None or earlier is None else direction * math.log(now_close[1] / earlier[1]) / sigma
        if event.event_type == "absorption" and event.payload.get("absorbed_side") == entry_side:
            label = "absorption_against"
        elif own is not None and own >= settings.pre_move_veto_sigma:
            label = "climax"
        elif own is not None and own < settings.initiative_max_sigma:
            label = "initiative"
        else:
            label = "other"
        recent = event.published_at > climax_from
        if recent and ((label == "climax" and relation == "same") or label == "absorption_against"
                       or (label == "initiative" and relation == "opposite")):
            counts = "veto"
        elif label == "initiative" and relation == "same" and event.event_type in INITIATIVE_TYPES:
            counts = "confirm"
        else:
            counts = "none"
        evidence.append(Evidence(event.event_id, event.event_type, event.direction or "", relation,
                                 event.published_at, own, label, counts, event.payload))

    against = [item for item in evidence if item.counts_for == "veto"]
    support = [item for item in evidence if item.counts_for == "confirm"]
    if pre >= settings.pre_move_veto_sigma and against:
        verdict = VETO_STRONG if position_side >= settings.extreme_position else VETO
        reason = "climax"
    elif support:
        verdict, reason = CONFIRM, "initiative"
    else:
        verdict, reason = NEUTRAL, "no_evidence"

    inputs = {
        "sigma15_bps": round(sigma * 1e4, 4),
        "sigma_days": sigma_days_found,
        "sigma_values": sigma_n,
        "pre_move_sigma": round(pre, 4),
        "range_position": round(position_side, 4),
        "close_at_entry": {"open_time_utc": at_entry[0].isoformat() + "Z", "close": at_entry[1]},
        "close_15m_before": {"open_time_utc": before[0].isoformat() + "Z", "close": before[1]},
        "range_2h": {"high": high, "low": low},
        "thresholds": {
            "pre_move_veto_sigma": settings.pre_move_veto_sigma,
            "initiative_max_sigma": settings.initiative_max_sigma,
            "extreme_position": settings.extreme_position,
            "climax_window_min": settings.climax_window_min,
            "initiative_window_min": settings.initiative_window_min,
            "enforce_level": settings.enforce_level,
        },
        "evidence": [item.as_dict(t) for item in evidence],
    }
    message = _message(verdict, mode, pre, position_side, settings, against, support)
    return ClusterVerdict(verdict, reason, message, mode, inputs, tuple(evidence))


def _message(verdict: str, mode: str, pre: float, position: float, settings: ClusterSettings,
             against: Sequence[Evidence], support: Sequence[Evidence]) -> str:
    if verdict in (VETO, VETO_STRONG):
        clue = against[-1]
        own = "" if clue.own is None else _ru(clue.own, signed=True) + "σ"
        clue_text = f"{_clock(clue.published_at)} {_kind(clue)}"
        if own:
            clue_text += f" после хода {own}" if clue.label == "climax" else f" (ход {own})"
        move = f"ход до входа {_ru(pre, signed=True)}σ (порог {_trim(settings.pre_move_veto_sigma)}σ)"
        if verdict == VETO_STRONG:
            core = f"вход на кульминации у края 2ч диапазона ({round(position * 100)}%) · {move} · {clue_text}"
        else:
            place = "середина 2ч диапазона" if position >= 0.2 else "у противоположного края 2ч диапазона"
            core = f"вход на кульминации · {move} · {place} · {clue_text}"
        return ("запретил бы — " + core) if mode == MODE_SHADOW else core
    if verdict == CONFIRM:
        items = [f"{_clock(item.published_at)} {_kind(item)}"
                 + ("" if item.own is None else f" (ход {_ru(item.own, signed=True)}σ)") for item in support[-2:]]
        return "подтверждает — инициатива до движения · " + " · ".join(items)
    return "без признаков кульминации и инициативы"
