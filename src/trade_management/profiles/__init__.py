"""Trade-management profile contracts, declarations, and shared utilities."""

from src.trade_management.profiles.base import (
    ManagementContext,
    PlanningContext,
    ProfileResult,
    TradeManagementProfile,
)
from src.trade_management.profiles.levels_rr import LevelsRrProfile
from src.trade_management.profiles.atr_trend import AtrTrendProfile
from src.trade_management.profiles.ma_cloud import MaCloudProfile
from src.trade_management.profiles.pattern_targets import PatternTargetsProfile
from src.trade_management.profiles.registry import (
    ProfileDefinition,
    get_profile_definition,
    profile_names,
    validate_strategy_compatibility,
)

__all__ = [
    "ManagementContext",
    "AtrTrendProfile",
    "LevelsRrProfile",
    "MaCloudProfile",
    "PatternTargetsProfile",
    "PlanningContext",
    "ProfileDefinition",
    "ProfileResult",
    "TradeManagementProfile",
    "get_profile_definition",
    "profile_names",
    "validate_strategy_compatibility",
]
