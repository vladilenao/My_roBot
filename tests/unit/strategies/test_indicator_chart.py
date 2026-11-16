"""Описание отрисовки индикаторов для графика UI (add-indicators-api)."""

from dataclasses import fields

import pytest

from src.assembly.contour import _strategy_map
from src.strategies.indicators.bb import BollingerBandsIndicator
from src.strategies.indicators.chart import ChartLine, IndicatorChart
from src.strategies.indicators.ma import MaCloudIndicator
from src.strategies.indicators.macd import MacdIndicator
from src.strategies.indicators.rsi import RsiIndicator
from src.strategies.indicators.stochastic import StochasticIndicator
from tests.snapshot.helper import load_candles_fixture

CONFIGURED = [indicator for config in _strategy_map().values() for indicator in config.indicators]


@pytest.fixture(scope="module")
def candles():
    return load_candles_fixture("NG_15m").tail(300).reset_index(drop=True)


@pytest.mark.parametrize("indicator", CONFIGURED, ids=repr)
def test_chart_columns_exist_in_compute(indicator, candles):
    result = indicator.compute(candles)
    chart = indicator.chart
    assert chart.lines and all(line.column in result.columns for line in chart.lines)
    assert chart.pane in ("price", "separate")
    if chart.fill is not None:
        assert set(chart.fill) <= {line.name for line in chart.lines}


def test_every_strategy_indicator_kind_is_covered():
    kinds = {type(indicator) for indicator in CONFIGURED}
    assert kinds == {MacdIndicator, RsiIndicator, StochasticIndicator, MaCloudIndicator, BollingerBandsIndicator}


def test_no_colors_in_description():
    """Цвета — дело UI: в описании только роли линий."""
    names = {field.name for cls in (IndicatorChart, ChartLine) for field in fields(cls)}
    assert not any("color" in name or "colour" in name for name in names)


def test_macd_description():
    chart = MacdIndicator(fast=12, slow=26, signal=9).chart
    assert (chart.key, chart.title, chart.pane, chart.levels) == ("macd", "MACD 12/26/9", "separate", (0.0,))
    assert chart.lines == (ChartLine("macd", "macd_12_26_9"), ChartLine("signal", "macds_12_26_9"),
                           ChartLine("histogram", "macdh_12_26_9", "histogram"))
    assert chart.params == {"fast": 12, "slow": 26, "signal": 9}


def test_ma_cloud_on_price_with_fill():
    chart = MaCloudIndicator(fast_period=10, slow_period=40).chart
    assert (chart.pane, chart.fill) == ("price", ("fast", "slow"))
    assert [(line.name, line.column) for line in chart.lines] == [("fast", "sma_fast"), ("slow", "sma_slow")]


def test_levels_of_oscillators():
    assert RsiIndicator(period=14).chart.levels == (30.0, 50.0, 70.0)
    assert StochasticIndicator(k=14, d=3, smooth_k=3).chart.levels == (20.0, 80.0)
    assert BollingerBandsIndicator().chart.levels == ()
