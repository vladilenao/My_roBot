"""Volatility-bounded geometry of a planned protective stop.

A profile says how far its own reference sits from the entry — a support level,
a pattern point, an ATR multiple — but it cannot know whether that distance is
tradeable.  A stop three ticks wide is inside the noise of the bar that created
it; a stop eight ATRs wide makes its own targets unreachable.  This module owns
the single rule that turns a proposed distance into the one the plan records, so
every profile is bounded the same way and no new profile can opt out.

Everything here is a pure function of prices already present in the local market
view: no broker, no storage, no history beyond the last bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from src.trade_management.models import StopBasis
from src.trade_management.profiles.rules import initial_stop, validate_price

ZERO = Decimal("0")

# Defaults keep protection out of the noise of its own bar without demanding a
# distance the instrument cannot express in ticks, and bound the other end so a
# far-away level cannot make the plan's own targets unreachable.
DEFAULT_MIN_STOP_ATR = Decimal("1.5")
DEFAULT_MIN_STOP_TICKS = Decimal("10")
DEFAULT_STOP_BEYOND_BAR = Decimal("1")
DEFAULT_MAX_STOP_ATR = Decimal("6")

DEFAULTS: Mapping[str, Decimal] = {
    "min_stop_atr": DEFAULT_MIN_STOP_ATR,
    "min_stop_ticks": DEFAULT_MIN_STOP_TICKS,
    "stop_beyond_bar": DEFAULT_STOP_BEYOND_BAR,
    "max_stop_atr": DEFAULT_MAX_STOP_ATR,
}


@dataclass(frozen=True)
class StopPolicy:
    """Bounds a stop distance must satisfy, expressed in the profile's own units."""

    min_stop_atr: Decimal = DEFAULT_MIN_STOP_ATR
    min_stop_ticks: Decimal = DEFAULT_MIN_STOP_TICKS
    stop_beyond_bar: Decimal = DEFAULT_STOP_BEYOND_BAR
    max_stop_atr: Decimal | None = DEFAULT_MAX_STOP_ATR

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, object] | None) -> "StopPolicy":
        """Read the policy out of profile parameters, falling back to the defaults.

        A profile without explicit geometry still gets bounded: an absent key
        means "use the shipped bound", not "no bound".  Malformed values are
        rejected by the configuration loader long before planning, so an unusable
        key here is a programming error rather than user input.
        """
        values = dict(parameters or {})
        max_stop_atr = values.get("max_stop_atr", DEFAULTS["max_stop_atr"])
        return cls(
            min_stop_atr=_bound(values, "min_stop_atr"),
            min_stop_ticks=_bound(values, "min_stop_ticks"),
            stop_beyond_bar=_bound(values, "stop_beyond_bar"),
            max_stop_atr=None if max_stop_atr is None else _bound(
                {"max_stop_atr": max_stop_atr}, "max_stop_atr"
            ),
        )

    def floor(
        self,
        *,
        atr: Decimal | None,
        bar_range: Decimal | None,
        price_step: Decimal | None,
    ) -> Decimal:
        """Smallest distance the instrument's own movement justifies."""
        terms = [
            term
            for term in (
                self.min_stop_atr * atr if atr is not None else None,
                self.min_stop_ticks * price_step if price_step is not None else None,
                self.stop_beyond_bar * bar_range if bar_range is not None else None,
            )
            if term is not None and term > ZERO
        ]
        return max(terms) if terms else ZERO

    def cap(self, atr: Decimal | None) -> Decimal | None:
        """Largest distance that keeps the plan's targets reachable, if bounded."""
        if self.max_stop_atr is None or atr is None:
            return None
        return self.max_stop_atr * atr


@dataclass(frozen=True)
class StopGeometry:
    """The bounded distance and its price, plus why that distance was chosen."""

    distance: Decimal
    price: Decimal
    basis: StopBasis

    @property
    def adjusted(self) -> bool:
        """Whether the volatility bounds, rather than the profile, chose the stop."""
        return self.basis is not StopBasis.STRUCTURAL


class StopBoundsConflict(ValueError):
    """Incompatible pre-rounding bounds with reproducible numerical inputs."""

    def __init__(self, proposed, floor, cap, step):
        self.details = {"structural_distance": proposed, "floor": floor, "cap": cap, "price_step": step}
        super().__init__("stop-bounds-conflict")


def resolve_stop(
    entry: Decimal | int | str,
    side: str,
    structural_distance: Decimal | int | str,
    policy: StopPolicy,
    *,
    atr: Decimal | int | str | None = None,
    bar_range: Decimal | int | str | None = None,
    price_step: Decimal | int | str | None = None,
    reject_conflict: bool = False,
) -> StopGeometry:
    """Bound a proposed stop distance and price it on the loss side of entry.

    The distance becomes ``min(max(structural, floor), cap)``: never narrower than
    the volatility floor, never wider than the cap.  Both bounds are fed by ATR and
    the bar range, so an input that is unavailable withdraws the bound it feeds
    instead of withdrawing the plan — only a profile that cannot build a stop at
    all rejects, and that stays its own decision.

    The distance is never squeezed below one tick, because a stop that rounds onto
    the entry is not a stop; that guard can only bind under a configuration whose
    cap is narrower than a single tick of the instrument.
    """
    entry_price = validate_price(entry, "entry")
    proposed = _non_negative(structural_distance, "structural_distance")
    step = None if price_step is None else validate_price(price_step, "price_step")
    atr_value = None if atr is None else _non_negative(atr, "atr")
    range_value = None if bar_range is None else _non_negative(bar_range, "bar_range")

    floor = policy.floor(atr=atr_value, bar_range=range_value, price_step=step)
    cap = policy.cap(atr_value)
    if reject_conflict and cap is not None and (floor > cap or (step is not None and step > cap)):
        raise StopBoundsConflict(proposed, floor, cap, step)
    distance = max(proposed, floor)
    basis = StopBasis.ATR_FLOOR if distance > proposed else StopBasis.STRUCTURAL

    if cap is not None and distance > cap:
        distance, basis = cap, StopBasis.ATR_CAP
    if step is not None:
        distance = max(distance, step)

    raw = entry_price - distance if _is_long(side) else entry_price + distance
    stop_price = initial_stop(raw, step, side) if step is not None else validate_price(raw, "stop")
    return StopGeometry(distance=abs(entry_price - stop_price), price=stop_price, basis=basis)


def r_linked_ratios(parameters: Mapping[str, object] | None) -> dict[str, Decimal]:
    """Target multipliers a profile declares in risk units, keyed by target id.

    Only these targets are re-priced from a bounded stop.  A profile that names
    its targets in absolute terms — a pattern's midpoint and D — declares no R
    multiplier and keeps the prices the formation gave it: a stop that moves away
    from such a target does not make the target any less where the pattern put it.
    """
    values = dict(parameters or {})
    target_r = values.get("target_R")
    if isinstance(target_r, (list, tuple)) and target_r:
        return {
            f"tp-{index}": _non_negative(value, "target_R")
            for index, value in enumerate(target_r, start=1)
        }
    tp1_r = values.get("tp1_R")
    if tp1_r is not None:
        return {"tp-1": _non_negative(tp1_r, "tp1_R")}
    return {}


def _is_long(side: str) -> bool:
    normalized = str(side).upper()
    if normalized in {"BUY", "LONG"}:
        return True
    if normalized in {"SELL", "SHORT"}:
        return False
    raise ValueError("side must be BUY/LONG or SELL/SHORT")


def _non_negative(value: object, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - mirrors the shared price rule
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if result < ZERO:
        raise ValueError(f"{name} cannot be negative")
    return result


def _bound(values: Mapping[str, object], name: str) -> Decimal:
    return _non_negative(values.get(name, DEFAULTS[name]), name)
