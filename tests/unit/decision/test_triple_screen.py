import numpy as np
import pandas as pd
import pytest

from src.decision.filters.triple_screen import (
    TripleScreenFilter,
    TripleScreenParams,
    tf_hierarchy,
)
from src.strategies.contracts import Decision, SignalType

LADDER = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")


def _frame_from_close(close_values, freq="4h", start="2024-01-01"):
    closes = [float(c) for c in close_values]
    n = len(closes)
    return pd.DataFrame(
        {
            "datetime": pd.date_range(start, periods=n, freq=freq),
            "open": [closes[i - 1] if i else closes[0] for i in range(n)],
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }
    )


def _uptrend(n=70, start=100.0):
    """Ускоренный рост цены: гистограмма MACD растёт (экран 1 = BUY)."""
    return (start + 0.02 * np.arange(n) ** 2).tolist()


def _downtrend(n=70, start=200.0):
    """Ускоренное падение цены: гистограмма MACD падает (экран 1 = SELL)."""
    return (start - 0.02 * np.arange(n) ** 2).tolist()


def _oversold_tail():
    """Резкий обвал в хвосте: %K далеко ниже 20 (экран 2 = BUY)."""
    flat = [100.0] * 50
    return flat + [
        100.0, 99.0, 96.0, 91.0, 84.0, 80.0, 76.0, 73.0, 71.0, 70.0,
        69.5, 69.0, 68.8, 68.6, 68.4, 68.3, 68.2,
    ]


def _overbought_tail():
    """Резкий рост в хвосте: %K далеко выше 80 (экран 2 = SELL)."""
    flat = [100.0] * 50
    return flat + [
        100.0, 101.0, 104.0, 109.0, 116.0, 120.0, 124.0, 127.0, 129.0, 130.0,
        130.5, 131.0, 131.2, 131.4, 131.6, 131.7, 131.8,
    ]


def _neutral_tail():
    """Мягкие колебания в хвосте: %K в нейтральной зоне (экран 2 = None)."""
    flat = [100.0] * 50
    return flat + [
        100.0, 100.4, 100.8, 101.2, 100.9, 100.7, 101.1, 101.0, 100.8, 101.3,
        101.0, 100.9, 101.2, 100.7, 101.1, 101.0, 101.2,
    ]


class FakeProvider:
    def __init__(self, frames):
        self.frames = dict(frames)
        self.calls = []

    def frame_for(self, instrument, timeframe, *, max_close=None, min_bars=0):
        self.calls.append(
            {
                "instrument": instrument,
                "timeframe": timeframe,
                "max_close": max_close,
                "min_bars": min_bars,
            }
        )
        return self.frames.get(timeframe, pd.DataFrame()).copy()


def _decision(signal_type):
    return Decision(
        signal_type=signal_type,
        price=100.0,
        bar_time=pd.Timestamp("2024-03-08 16:00"),
    )


def _triple_filter(frames, **kwargs):
    kwargs.setdefault("params", TripleScreenParams())
    kwargs.setdefault("ladder", LADDER)
    return TripleScreenFilter(provider=FakeProvider(frames), **kwargs)


class TestTripleScreenParams:
    def test_defaults(self):
        params = TripleScreenParams()
        assert params.multiplier == 5
        assert (params.macd_fast, params.macd_slow, params.macd_signal) == (12, 26, 9)
        assert (params.stoch_k, params.stoch_d, params.stoch_smooth_k) == (14, 3, 3)
        assert (params.oversold, params.overbought) == (20, 80)

    def test_from_config_overrides(self):
        params = TripleScreenParams.from_config(
            {"multiplier": 3, "oversold": 25, "macd_fast": 5}
        )
        assert params.multiplier == 3
        assert params.oversold == 25
        assert params.macd_fast == 5
        assert params.macd_slow == 26  # незатронутые значения остаются дефолтными

    def test_from_config_empty(self):
        assert TripleScreenParams.from_config({}) == TripleScreenParams()

    def test_from_config_unknown_key(self):
        with pytest.raises(ValueError, match="Незнакомые ключи"):
            TripleScreenParams.from_config({"window": 5})

    def test_multiplier_below_two_rejected(self):
        with pytest.raises(ValueError):
            TripleScreenParams(multiplier=1)

    def test_macd_fast_not_below_slow_rejected(self):
        with pytest.raises(ValueError):
            TripleScreenParams(macd_fast=26, macd_slow=12)

    def test_macd_signal_not_below_slow_rejected(self):
        with pytest.raises(ValueError):
            TripleScreenParams(macd_signal=26)

    def test_oversold_not_below_overbought_rejected(self):
        with pytest.raises(ValueError):
            TripleScreenParams(oversold=90, overbought=80)

    def test_zone_bounds_rejected(self):
        with pytest.raises(ValueError):
            TripleScreenParams(oversold=0)
        with pytest.raises(ValueError):
            TripleScreenParams(overbought=100)


class TestTfHierarchy:
    def test_default_multiplier_5m(self):
        assert tf_hierarchy("5m", 5, LADDER) == ("30m", "4h")

    def test_default_multiplier_15m(self):
        # 15m × 5 = 75 мин → строго больше ближайший 4h; 4h × 5 = 20ч → 1d
        assert tf_hierarchy("15m", 5, LADDER) == ("4h", "1d")

    def test_default_multiplier_1m(self):
        assert tf_hierarchy("1m", 5, LADDER) == ("15m", "4h")

    def test_multiplier_3(self):
        assert tf_hierarchy("1m", 3, LADDER) == ("5m", "30m")
        assert tf_hierarchy("15m", 3, LADDER) == ("1h", "4h")

    def test_nearest_strictly_greater(self):
        assert tf_hierarchy("5m", 5, ("5m", "4h", "1d")) == ("4h", "1d")

    def test_ladder_order_independent(self):
        assert tf_hierarchy("5m", 5, tuple(reversed(LADDER))) == ("30m", "4h")

    def test_unknown_work_tf(self):
        with pytest.raises(ValueError):
            tf_hierarchy("3m", 5, LADDER)

    def test_month_work_tf_rejected(self):
        # "1M" (месяц): шаг 1M × 5 превышает лестницу TIMEFRAMES — несовместимо.
        with pytest.raises(ValueError):
            tf_hierarchy("1M", 5, LADDER)


class TestTripleScreenFilter:
    def test_hold_passthrough(self):
        provider = FakeProvider({})
        flt = TripleScreenFilter(provider=provider, params=TripleScreenParams(), ladder=LADDER)
        decision = _decision(SignalType.HOLD)

        result = flt.apply(decision, ctx=None, instrument="SBER", timeframe="5m")
        assert result is decision
        assert provider.calls == []

    def test_buy_confirmed_by_both_screens(self):
        frames = {
            "30m": _frame_from_close(_oversold_tail(), freq="30min"),
            "4h": _frame_from_close(_uptrend(), freq="4h"),
        }
        flt = _triple_filter(frames)
        result = flt.apply(_decision(SignalType.BUY), ctx=None, instrument="SBER", timeframe="5m")
        assert result.signal_type is SignalType.BUY

    def test_sell_confirmed_by_both_screens(self):
        frames = {
            "30m": _frame_from_close(_overbought_tail(), freq="30min"),
            "4h": _frame_from_close(_downtrend(), freq="4h"),
        }
        flt = _triple_filter(frames)
        result = flt.apply(_decision(SignalType.SELL), ctx=None, instrument="SBER", timeframe="5m")
        assert result.signal_type is SignalType.SELL

    def test_buy_blocked_when_screen2_neutral(self):
        frames = {
            "30m": _frame_from_close(_neutral_tail(), freq="30min"),
            "4h": _frame_from_close(_uptrend(), freq="4h"),
        }
        # экран 1 BUY (тренд вверх), но %K в нейтральной зоне — нет подтверждения коррекции
        flt = _triple_filter(frames)
        result = flt.apply(_decision(SignalType.BUY), ctx=None, instrument="SBER", timeframe="5m")
        assert result.signal_type is SignalType.HOLD

    def test_buy_blocked_when_screen1_bear(self):
        frames = {
            "30m": _frame_from_close(_oversold_tail(), freq="30min"),
            "4h": _frame_from_close(_downtrend(), freq="4h"),
        }
        # экран 2 BUY (перепроданность), но гистограмма старшего ТФ падает — направление против
        flt = _triple_filter(frames)
        result = flt.apply(_decision(SignalType.BUY), ctx=None, instrument="SBER", timeframe="5m")
        assert result.signal_type is SignalType.HOLD

    def test_sell_blocked_when_screen1_bull(self):
        frames = {
            "30m": _frame_from_close(_overbought_tail(), freq="30min"),
            "4h": _frame_from_close(_uptrend(), freq="4h"),
        }
        flt = _triple_filter(frames)
        result = flt.apply(_decision(SignalType.SELL), ctx=None, instrument="SBER", timeframe="5m")
        assert result.signal_type is SignalType.HOLD

    def test_hold_when_no_binding_context(self):
        provider = FakeProvider({})
        flt = TripleScreenFilter(provider=provider, params=TripleScreenParams(), ladder=LADDER)
        result = flt.apply(_decision(SignalType.BUY), ctx=None, instrument="", timeframe="")
        assert result.signal_type is SignalType.HOLD
        assert provider.calls == []

    def test_hold_when_htf_frame_too_short(self):
        frames = {
            "30m": _frame_from_close(_oversold_tail(), freq="30min"),
            "4h": _frame_from_close(_uptrend(n=20), freq="4h"),
        }
        flt = _triple_filter(frames)
        result = flt.apply(_decision(SignalType.BUY), ctx=None, instrument="SBER", timeframe="5m")
        assert result.signal_type is SignalType.HOLD

    def test_hold_when_ladder_step_overflows(self):
        provider = FakeProvider({})
        flt = TripleScreenFilter(provider=provider, params=TripleScreenParams(), ladder=LADDER)
        # 1d × 5 → 1w, 1w × 5 выходит за лестницу — сигнал отклоняется без запросов данных
        result = flt.apply(_decision(SignalType.BUY), ctx=None, instrument="SBER", timeframe="1d")
        assert result.signal_type is SignalType.HOLD
        assert provider.calls == []

    def test_provider_receives_closed_window_and_warmup(self):
        frames = {
            "30m": _frame_from_close(_oversold_tail(), freq="30min"),
            "4h": _frame_from_close(_uptrend(), freq="4h"),
        }
        flt = _triple_filter(frames)
        decision = _decision(SignalType.BUY)
        flt.apply(decision, ctx=None, instrument="SBER", timeframe="5m")

        assert len(provider_calls := flt.provider.calls) == 2
        htf_call = next(c for c in provider_calls if c["timeframe"] == "4h")
        int_call = next(c for c in provider_calls if c["timeframe"] == "30m")
        assert htf_call["max_close"] == decision.bar_time
        assert int_call["max_close"] == decision.bar_time
        # MACD warmup = slow + signal = 35 → запрос на 37 баров; Stochastic warmup = 17 → 18 баров
        assert htf_call["min_bars"] == 37
        assert int_call["min_bars"] == 18