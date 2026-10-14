from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class PatternResult:
    event_type: str
    direction: str
    previous_candle: Mapping[str, object]
    current_candle: Mapping[str, object]
    body_overlap_ratio: float


class BasePattern(Protocol):
    event_type: str
    direction: str

    def analyze(self, candles: Sequence[Mapping[str, object]]) -> PatternResult | None: ...


def body_bounds(candle: Mapping[str, object]) -> tuple[float, float]:
    opening, closing = float(candle["open"]), float(candle["close"])
    return min(opening, closing), max(opening, closing)


def engulfing_result(pattern: BasePattern, previous: Mapping[str, object],
                     current: Mapping[str, object]) -> PatternResult:
    previous_low, previous_high = body_bounds(previous)
    current_low, current_high = body_bounds(current)
    previous_body = previous_high - previous_low
    overlap = max(0.0, min(previous_high, current_high) - max(previous_low, current_low))
    return PatternResult(
        event_type=pattern.event_type,
        direction=pattern.direction,
        previous_candle=previous,
        current_candle=current,
        body_overlap_ratio=min(1.0, overlap / previous_body),
    )
