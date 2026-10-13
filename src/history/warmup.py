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
    for binding in bindings:
        strategy = get_strategy(binding.strategy, strategies[binding.strategy])
        for tf, count in binding_requirements(binding, strategy, profiles, triple_screen, ladder).items():
            requirements[tf] = max(requirements.get(tf, 0), count)
    return requirements


def management_requirement(parameters):
    """Прогрев сопровождения профиля: ATR и медленная MA плюс запас."""
    return max(15, parameters.get("atr_period", 14)+1, parameters.get("ma_slow_period", 0)+1)


def obligation_requirements(trades):
    """ТФ сопровождения сохранённых сделок: удаление привязки их не отменяет."""
    requirements = {}
    for trade in trades:
        plan = trade.plan
        if not plan.timeframe:
            continue
        count = management_requirement(dict(plan.profile.parameters))
        requirements[plan.timeframe] = max(requirements.get(plan.timeframe, 0), count)
    if requirements:
        requirements["1m"] = max(requirements.get("1m", 0), 1)
    return requirements


def binding_requirements(binding, strategy, profiles, triple_screen, ladder):
    trend, sr = TrendAnalyzer(), SRLevelsCalculator()
    profile = profiles.get(binding.management, {})
    management = management_requirement(profile)
    required = {binding.timeframe: max(strategy.required_history(), management,
        trend.ema_long, trend.adx_period*2, sr.fractal_bars)}
    if binding.filter_profile == "triple_screen":
        middle, higher = tf_hierarchy(binding.timeframe, triple_screen.multiplier, ladder)
        macd = MacdIndicator(fast=triple_screen.macd_fast, slow=triple_screen.macd_slow, signal=triple_screen.macd_signal)
        stoch = StochasticIndicator(k=triple_screen.stoch_k, d=triple_screen.stoch_d, smooth_k=triple_screen.stoch_smooth_k)
        required[middle], required[higher] = stoch.warmup+1, macd.warmup+2
    return required


def live_data_requirements(bindings, strategies, profiles, triple_screen, ladder, *, saved_requirements=None):
    """Union требований одного UID для main-thread roster, без финансовой БД.

    Сохранённые потребности сопровождения передаёт вызывающая composition из
    своего ledger/lifecycle; удалённая входная привязка не удаляет этот ТФ.
    Один UID/ТФ имеет максимальный необходимый прогрев, не копию на binding.
    """
    requirements = {"1m": 1}
    for timeframe, count in (saved_requirements or {}).items():
        if timeframe not in ladder or isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("Некорректное сохранённое требование live-данных")
        requirements[timeframe] = max(requirements.get(timeframe, 0), count)
    for binding in bindings:
        strategy = get_strategy(binding.strategy, strategies[binding.strategy])
        for tf, count in binding_requirements(binding, strategy, profiles, triple_screen, ladder).items():
            requirements[tf] = max(requirements.get(tf, 0), count)
    return requirements
