import pytest

from src.strategies.indicators.macd import MacdIndicator, MacdIndicatorBuilder
from src.strategies.indicators.rsi import RsiIndicator, RsiIndicatorBuilder
from src.strategies.indicators.stochastic import (
    StochasticIndicator,
    StochasticIndicatorBuilder,
)
from src.strategies.strategy import StrategyBuilder, StrategyConfig


class TestMacdIndicator:
    def test_defaults(self):
        ind = MacdIndicator()
        assert ind.fast == 12
        assert ind.slow == 26
        assert ind.signal == 9
        assert ind.signal_column == "macd_signal"

    def test_warmup(self):
        ind = MacdIndicator(fast=8, slow=17, signal=5)
        assert ind.warmup == 22

    def test_frozen(self):
        ind = MacdIndicator()
        with pytest.raises(Exception):
            ind.fast = 20

    def test_validation_fast_gte_slow(self):
        with pytest.raises(ValueError, match="fast.*<.*slow"):
            MacdIndicator(fast=26, slow=12)

    def test_validation_signal_gte_slow(self):
        with pytest.raises(ValueError, match="signal.*<.*slow"):
            MacdIndicator(fast=5, slow=12, signal=12)


class TestMacdIndicatorBuilder:
    def test_build_with_defaults(self):
        ind = MacdIndicatorBuilder().build()
        assert ind.fast == 12
        assert ind.slow == 26
        assert ind.signal == 9

    def test_build_with_custom_params(self):
        ind = MacdIndicatorBuilder().set_fast(8).set_slow(17).set_signal(5).build()
        assert ind.fast == 8
        assert ind.slow == 17
        assert ind.signal == 5

    def test_chaining_returns_builder(self):
        builder = MacdIndicatorBuilder()
        result = builder.set_fast(8)
        assert result is builder


class TestRsiIndicator:
    def test_defaults(self):
        ind = RsiIndicator()
        assert ind.period == 14
        assert ind.signal_column == "rsi_signal"

    def test_warmup(self):
        ind = RsiIndicator(period=21)
        assert ind.warmup == 21

    def test_frozen(self):
        ind = RsiIndicator()
        with pytest.raises(Exception):
            ind.period = 7

    def test_validation_period_lte_zero(self):
        with pytest.raises(ValueError, match="period.*>.*0"):
            RsiIndicator(period=0)


class TestRsiIndicatorBuilder:
    def test_build_with_defaults(self):
        ind = RsiIndicatorBuilder().build()
        assert ind.period == 14

    def test_build_with_custom_period(self):
        ind = RsiIndicatorBuilder().set_period(7).build()
        assert ind.period == 7


class TestStochasticIndicator:
    def test_defaults(self):
        ind = StochasticIndicator()
        assert ind.k == 14
        assert ind.d == 3
        assert ind.smooth_k == 3
        assert ind.signal_column == "stoch_signal"

    def test_warmup(self):
        ind = StochasticIndicator(k=9, d=3, smooth_k=3)
        assert ind.warmup == 12

    def test_frozen(self):
        ind = StochasticIndicator()
        with pytest.raises(Exception):
            ind.k = 21

    def test_validation_k_lt_d(self):
        with pytest.raises(ValueError, match="k.*>=.*d"):
            StochasticIndicator(k=3, d=14)


class TestStochasticIndicatorBuilder:
    def test_build_with_defaults(self):
        ind = StochasticIndicatorBuilder().build()
        assert ind.k == 14
        assert ind.d == 3
        assert ind.smooth_k == 3

    def test_build_with_custom_params(self):
        ind = StochasticIndicatorBuilder().set_k(9).set_d(3).set_smooth_k(3).build()
        assert ind.k == 9
        assert ind.d == 3
        assert ind.smooth_k == 3


class TestStrategyConfig:
    def test_required_history_with_indicators(self):
        config = StrategyConfig(
            name="test",
            strategy_window=5,
            indicators=(
                MacdIndicator(),
                RsiIndicator(),
                StochasticIndicator(),
            ),
        )
        assert config.required_history == 40

    def test_required_history_no_indicators(self):
        config = StrategyConfig(
            name="test",
            strategy_window=5,
            indicators=(),
        )
        assert config.required_history == 5

    def test_signal_columns(self):
        config = StrategyConfig(
            name="test",
            strategy_window=5,
            indicators=(
                MacdIndicator(),
                RsiIndicator(),
                StochasticIndicator(),
            ),
        )
        assert config.signal_columns == ["macd_signal", "rsi_signal", "stoch_signal"]

    def test_frozen(self):
        config = StrategyConfig(name="test", strategy_window=5, indicators=())
        with pytest.raises(Exception):
            config.name = "other"


class TestStrategyBuilder:
    def test_build_with_all_params(self):
        config = (
            StrategyBuilder()
            .set_name("test_strategy")
            .set_strategy_window(3)
            .add_indicator(MacdIndicatorBuilder().set_fast(8).set_slow(17).set_signal(5).build())
            .add_indicator(RsiIndicatorBuilder().set_period(7).build())
            .add_indicator(StochasticIndicatorBuilder().set_k(9).set_d(3).set_smooth_k(3).build())
            .build()
        )
        assert config.name == "test_strategy"
        assert config.strategy_window == 3
        assert len(config.indicators) == 3
        assert isinstance(config.indicators[0], MacdIndicator)
        assert isinstance(config.indicators[1], RsiIndicator)
        assert isinstance(config.indicators[2], StochasticIndicator)

    def test_build_with_defaults(self):
        config = (
            StrategyBuilder()
            .set_name("default_test")
            .add_indicator(MacdIndicatorBuilder().build())
            .add_indicator(RsiIndicatorBuilder().build())
            .add_indicator(StochasticIndicatorBuilder().build())
            .build()
        )
        assert config.strategy_window == 5

    def test_validation_name_required(self):
        with pytest.raises(ValueError, match="Имя стратегии обязательно"):
            StrategyBuilder().build()

    def test_validation_window_positive(self):
        with pytest.raises(ValueError, match="strategy_window.*>.*0"):
            StrategyBuilder().set_name("test").set_strategy_window(0).build()

    def test_validation_at_least_one_indicator(self):
        with pytest.raises(ValueError, match="хотя бы один индикатор"):
            StrategyBuilder().set_name("test").build()

    def test_chaining(self):
        builder = StrategyBuilder()
        result = builder.set_name("test")
        assert result is builder
