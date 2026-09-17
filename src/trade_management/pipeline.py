"""Builders that turn live frames and account configuration into profile inputs.

Profiles stay pure and stateless; assembling their local ``market`` view, the
profile snapshot, and the concrete profile class lives here and in the trade
manager.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping

import pandas as pd

from src.market_context.models import MarketContext
from src.strategies.indicators.atr.indicator import AtrWilderIndicator
from src.strategies.indicators.ma.indicator import MaCloudIndicator
from src.trade_management.models import ProfileSnapshot
from src.trade_management.profiles.atr_trend import AtrTrendProfile
from src.trade_management.profiles.levels_rr import LevelsRrProfile
from src.trade_management.profiles.ma_cloud import MaCloudProfile
from src.trade_management.profiles.pattern_targets import PatternTargetsProfile


PROFILE_CLASSES: dict[str, type] = {
    LevelsRrProfile.NAME: LevelsRrProfile,
    AtrTrendProfile.NAME: AtrTrendProfile,
    MaCloudProfile.NAME: MaCloudProfile,
    PatternTargetsProfile.NAME: PatternTargetsProfile,
}


def build_profile_snapshot(
    name: str, parameters: Mapping[str, object] | None
) -> ProfileSnapshot:
    """Build the frozen profile snapshot registered in the durable plan."""
    if name not in PROFILE_CLASSES:
        raise ValueError(f"unknown profile {name!r}")
    return ProfileSnapshot(name, "1", dict(parameters or {}))


def build_management_market(
    *,
    contract: object | None,
    frame,
    context: MarketContext | None = None,
    profile_parameters: Mapping[str, object] | None = None,
    price: float | Decimal | None = None,
    signal: bool = True,
    commission: Decimal | float | str | None = None,
    slippage: Decimal | float | str | None = None,
) -> dict[str, object]:
    """Assemble the local market view consumed by trade-management profiles.

    ``price`` authorizes nearest-support/resistance selection and, when
    ``signal=True``, the same-side add candidate.  The two costs default to
    zero so a profile without configured commission still plans cleanly.
    """
    params = dict(profile_parameters or {})
    price_step = _price(getattr(contract, "price_step", None))
    step_cost = _price(getattr(contract, "step_cost", None))
    decision_price = _price(price)

    market: dict[str, object] = {"price_step": price_step, "step_cost": step_cost}
    if frame is not None and len(frame):
        bar_time = frame["datetime"].iloc[-1]
        if hasattr(bar_time, "to_pydatetime"):
            bar_time = bar_time.to_pydatetime()
        market["close"] = _price(frame["close"].iloc[-1])
        market["high"] = _price(frame["high"].iloc[-1])
        market["low"] = _price(frame["low"].iloc[-1])
        market["bar_id"] = str(bar_time)
        market["created_at"] = bar_time if isinstance(bar_time, datetime) else None

        atr_period = int(params.get("atr_period", 14))
        atr_frame = AtrWilderIndicator(period=atr_period).compute(frame)
        market["atr"] = _price(atr_frame["atr_wilder"].iloc[-1])

        fast_period = int(params.get("ma_fast_period", 10))
        slow_period = int(params.get("ma_slow_period", 40))
        if len(frame) >= max(fast_period, slow_period):
            ma_frame = MaCloudIndicator(
                fast_period=fast_period, slow_period=slow_period
            ).compute(frame)
            market["ma10"] = _price(ma_frame["sma_fast"].iloc[-1])
            market["ma40"] = _price(ma_frame["sma_slow"].iloc[-1])
        else:
            market["ma10"] = None
            market["ma40"] = None

    if context is not None and decision_price is not None:
        levels = list(getattr(context, "sr_levels", None) or ())
        below = sorted(
            (_price(level.price) for level in levels if _price(level.price) is not None and
             _price(level.price) < decision_price)
        )
        above = sorted(
            (_price(level.price) for level in levels if _price(level.price) is not None and
             _price(level.price) > decision_price)
        )
        market["support"] = below[-1] if below else None
        market["resistance"] = above[0] if above else None

    market["entry_cost"] = _price(commission) if commission is not None else Decimal("0")
    market["exit_cost"] = _price(commission) if commission is not None else Decimal("0")
    market["slippage_cost"] = _price(slippage) if slippage is not None else Decimal("0")

    if signal and decision_price is not None:
        market["add_signal_price"] = decision_price
    return market


def _price(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return Decimal(str(value))