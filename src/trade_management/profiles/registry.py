"""Known trade-management profiles and their strategy requirements."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ProfileDefinition:
    """A profile name and the strategy capabilities it requires."""

    name: str
    required_strategy_capabilities: frozenset[str] = frozenset()


_PROFILES: Mapping[str, ProfileDefinition] = MappingProxyType(
    {
        "levels_rr": ProfileDefinition("levels_rr"),
        "atr_trend": ProfileDefinition("atr_trend"),
        "ma_cloud": ProfileDefinition("ma_cloud"),
        "pattern_targets": ProfileDefinition(
            "pattern_targets", frozenset({"pattern_context"})
        ),
    }
)


def profile_names() -> tuple[str, ...]:
    """Return the stable, sorted set of profile names accepted by configuration."""
    return tuple(sorted(_PROFILES))


def get_profile_definition(name: str) -> ProfileDefinition:
    """Return a known profile declaration or fail before any trading work."""
    try:
        return _PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown trade-management profile {name!r}. "
            f"Available: {', '.join(profile_names())}"
        ) from exc


def validate_strategy_compatibility(
    profile_name: str, strategy_capabilities: Iterable[str]
) -> ProfileDefinition:
    """Validate that a strategy can supply all data required by a profile."""
    definition = get_profile_definition(profile_name)
    available = frozenset(strategy_capabilities)
    missing = definition.required_strategy_capabilities - available
    if missing:
        raise ValueError(
            f"Profile {profile_name!r} requires strategy capabilities: "
            f"{', '.join(sorted(missing))}"
        )
    return definition
