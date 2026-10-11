"""Требования прогрева конкретного семейства, включая старшие фильтры."""

from src.decision.filters.triple_screen import tf_hierarchy
from src.instruments.futures import FAMILIES
from src.market_context.trend import TrendAnalyzer
from src.market_context.sr_levels import SRLevelsCalculator
from src.strategies.indicators.macd.indicator import MacdIndicator
from src.strategies.indicators.stochastic.indicator import StochasticIndicator
from src.strategies.registry import get_strategy


def warmup_requirements(family, assignments, strategies, profiles, triple_screen, ladder):
    bindings = assignments.get(family, assignments.get(FAMILIES[family][0], []))
    requirements = {"1m": 1}
    trend, sr = TrendAnalyzer(), SRLevelsCalculator()
    for binding in bindings:
        strategy = get_strategy(binding.strategy, strategies[binding.strategy])
        profile = profiles.get(binding.management, {})
        management = max(15, profile.get("atr_period", 14)+1, profile.get("ma_slow_period", 0)+1)
        count = max(strategy.required_history(), management, trend.ema_long,
                    trend.adx_period*2, sr.fractal_bars)
        requirements[binding.timeframe] = max(requirements.get(binding.timeframe, 0), count)
        if binding.filter_profile == "triple_screen":
            middle, higher = tf_hierarchy(binding.timeframe, triple_screen.multiplier, ladder)
            macd = MacdIndicator(fast=triple_screen.macd_fast, slow=triple_screen.macd_slow, signal=triple_screen.macd_signal)
            stoch = StochasticIndicator(k=triple_screen.stoch_k, d=triple_screen.stoch_d, smooth_k=triple_screen.stoch_smooth_k)
            requirements[middle] = max(requirements.get(middle, 0), stoch.warmup+1)
            requirements[higher] = max(requirements.get(higher, 0), macd.warmup+2)
    return requirements
