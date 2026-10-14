from __future__ import annotations

from typing import Mapping, Sequence

from .base import PatternResult, body_bounds, engulfing_result


class BearishEngulfing:
    event_type = "bearish_engulfing"
    direction = "short"

    def analyze(self, candles: Sequence[Mapping[str, object]]) -> PatternResult | None:
        if len(candles) < 2:
            return None
        previous, current = candles[-2:]
        if not (float(previous["close"]) > float(previous["open"]) and
                float(current["close"]) < float(current["open"])):
            return None
        previous_low, previous_high = body_bounds(previous)
        current_low, current_high = body_bounds(current)
        if current_low <= previous_low and current_high >= previous_high:
            return engulfing_result(self, previous, current)
        return None
