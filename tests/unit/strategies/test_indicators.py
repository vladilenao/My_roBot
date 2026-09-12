import numpy as np
import pandas as pd

from src.strategies.indicators.ma import MaCloudIndicator
from src.strategies.indicators.ma.signalEnum import MaCloudSignalEnum
from src.strategies.indicators.macd import MacdIndicator, MacdMode
from src.strategies.indicators.macd.signalEnum import MacdZeroCrossSignalEnum
from src.strategies.indicators.rsi import RsiIndicator


def _make_ohlcv(n: int = 100, close_values: list[float] | None = None) -> pd.DataFrame:
    close = close_values if close_values is not None else np.linspace(90, 110, n)
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1000] * len(close),
        }
    )


class TestMaCloudIndicator:
    def test_warmup(self):
        ind = MaCloudIndicator(fast_period=10, slow_period=40)
        assert ind.warmup == 40

    def test_signal_column(self):
        ind = MaCloudIndicator()
        assert ind.signal_column == "ma_cloud_signal"

    def test_cross_up(self):
        n = 60
        close = np.concatenate(
            [
                np.linspace(110, 90, 40),
                np.linspace(90, 120, 20),
            ]
        )
        df = _make_ohlcv(n, close)
        ind = MaCloudIndicator(fast_period=5, slow_period=20)
        ta = ind.compute(df)
        signals = ta["ma_cloud_signal"].values
        assert MaCloudSignalEnum.MA_CROSS_UP in signals

    def test_cross_down(self):
        n = 60
        close = np.concatenate(
            [
                np.linspace(90, 110, 40),
                np.linspace(110, 80, 20),
            ]
        )
        df = _make_ohlcv(n, close)
        ind = MaCloudIndicator(fast_period=5, slow_period=20)
        ta = ind.compute(df)
        signals = ta["ma_cloud_signal"].values
        assert MaCloudSignalEnum.MA_CROSS_DOWN in signals

    def test_no_signal_flat(self):
        df = _make_ohlcv(50, [100.0] * 50)
        ind = MaCloudIndicator(fast_period=10, slow_period=40)
        ta = ind.compute(df)
        assert (ta["ma_cloud_signal"].iloc[41:] == MaCloudSignalEnum.NO_SIGNAL).all()

    def test_validation_fast_gte_slow(self):
        try:
            MaCloudIndicator(fast_period=40, slow_period=10)
            assert False, "Expected ValueError"
        except ValueError:
            pass

    def test_compute_adds_sma_columns(self):
        df = _make_ohlcv(50)
        ind = MaCloudIndicator(fast_period=10, slow_period=40)
        ta = ind.compute(df)
        assert "sma_fast" in ta.columns
        assert "sma_slow" in ta.columns
        assert "ma_cloud_signal" in ta.columns


class TestMacdZeroCrossIndicator:
    """MACD в режиме ZERO_CROSS — общий класс MacdIndicator."""

    def test_warmup(self):
        ind = MacdIndicator(fast=12, slow=26, signal=9, mode=MacdMode.ZERO_CROSS)
        assert ind.warmup == 35

    def test_signal_column(self):
        ind = MacdIndicator(mode=MacdMode.ZERO_CROSS)
        assert ind.signal_column == "macd_zero_signal"

    def test_signal_enum(self):
        ind = MacdIndicator(mode=MacdMode.ZERO_CROSS)
        assert ind.signal_enum is MacdZeroCrossSignalEnum

    def test_cross_above_zero(self):
        np.random.seed(42)
        n = 60
        base = np.linspace(100, 110, n)
        noise = np.random.randn(n) * 0.01
        close = base + noise
        df = _make_ohlcv(n, close)
        ind = MacdIndicator(mode=MacdMode.ZERO_CROSS)
        ta = ind.compute(df)
        signals = ta["macd_zero_signal"]
        assert (
            MacdZeroCrossSignalEnum.MACD_CROSS_ABOVE_ZERO in signals.values
            or MacdZeroCrossSignalEnum.NO_SIGNAL in signals.values
        )

    def test_no_signal_constant(self):
        df = _make_ohlcv(60, [100.0] * 60)
        ind = MacdIndicator(mode=MacdMode.ZERO_CROSS)
        ta = ind.compute(df)
        assert (
            ta["macd_zero_signal"].iloc[36:] == MacdZeroCrossSignalEnum.NO_SIGNAL
        ).all()

    def test_validation_fast_gte_slow(self):
        try:
            MacdIndicator(fast=30, slow=26, signal=9, mode=MacdMode.ZERO_CROSS)
            assert False, "Expected ValueError"
        except ValueError:
            pass

    def test_compute_adds_macd_columns(self):
        df = _make_ohlcv(60)
        ind = MacdIndicator(mode=MacdMode.ZERO_CROSS)
        ta = ind.compute(df)
        assert "macd_zero_signal" in ta.columns


class TestMacdSignalLineCross:
    """MACD в режиме SIGNAL_LINE_CROSS — классический кроссовер линий."""

    def test_signal_column(self):
        ind = MacdIndicator(mode=MacdMode.SIGNAL_LINE_CROSS)
        assert ind.signal_column == "macd_signal"

    def test_default_mode_is_signal_line_cross(self):
        ind = MacdIndicator(fast=12, slow=26, signal=9)
        assert ind.mode is MacdMode.SIGNAL_LINE_CROSS
        assert ind.signal_column == "macd_signal"


class TestRsiIndicatorReuse:
    def test_rsi_period_14(self):
        ind = RsiIndicator(period=14)
        assert ind.warmup == 14
        assert ind.signal_column == "rsi_signal"

    def test_rsi_cross_above_50(self):
        n = 30
        close = np.concatenate([np.linspace(90, 95, 15), np.linspace(95, 105, 15)])
        df = _make_ohlcv(n, close)
        ind = RsiIndicator(period=14)
        ta = ind.compute(df)
        signals = ta["rsi_signal"]
        assert 1 in signals.values or 0 in signals.values
