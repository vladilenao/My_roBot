import pytest
from src.signals.generator import make_decision, get_last_signals
import pandas as pd


class TestMakeDecision:
    def test_buy_without_label(self):
        result = make_decision(1, 1, 1, 100.0)
        assert "ПОКУПАТЬ" in result
        assert "100.0" in result
        assert "[" not in result

    def test_sell_without_label(self):
        result = make_decision(-1, -1, -1, 50.0)
        assert "ПРОДАВАТЬ" in result
        assert "50.0" in result
        assert "[" not in result

    def test_hold_without_label(self):
        result = make_decision(1, -1, 0, 50.0)
        assert "Отдыхаем" in result
        assert "[" not in result

    def test_buy_with_label(self):
        result = make_decision(1, 1, 1, 100.0, instrument_label="SBER share")
        assert result.startswith("[SBER share]")
        assert "ПОКУПАТЬ" in result
        assert "100.0" in result

    def test_sell_with_label(self):
        result = make_decision(-1, -1, -1, 50.0, instrument_label="NGU6 future")
        assert result.startswith("[NGU6 future]")
        assert "ПРОДАВАТЬ" in result
        assert "50.0" in result

    def test_hold_with_label(self):
        result = make_decision(1, -1, 0, 50.0, instrument_label="GAZP share")
        assert result.startswith("[GAZP share]")
        assert "Отдыхаем" in result

    def test_price_rounding(self):
        result = make_decision(1, 1, 1, 3.14159)
        assert "3.142" in result

    def test_price_rounding_sell(self):
        result = make_decision(-1, -1, -1, 3.14159)
        assert "3.142" in result

    def test_empty_label(self):
        result = make_decision(1, 1, 1, 100.0, instrument_label="")
        assert "[" not in result
        assert "ПОКУПАТЬ" in result

    def test_zero_signals_hold(self):
        result = make_decision(0, 0, 0, 100.0)
        assert "Отдыхаем" in result

    def test_mixed_signals_hold(self):
        result = make_decision(1, 1, -1, 100.0)
        assert "Отдыхаем" in result

    def test_label_with_multiple_spaces(self):
        result = make_decision(1, 1, 1, 100.0, instrument_label="SBER  share")
        assert result.startswith("[SBER  share]")


class TestGetLastSignals:
    def _make_df(self, macd_signals, rsi_signals, stoch_signals):
        return pd.DataFrame({
            'macd_signal': macd_signals,
            'rsi_signal': rsi_signals,
            'stoch_signal': stoch_signals,
        })

    def test_basic_window(self):
        df = self._make_df([1, 1, 1, 1, 1], [0, 0, 0, 0, 0], [-1, -1, -1, -1, -1])
        macd, rsi, stoch = get_last_signals(df, window=3)
        assert macd == 3
        assert rsi == 0
        assert stoch == -3

    def test_window_larger_than_data(self):
        df = self._make_df([1, 1], [0, 0], [-1, -1])
        macd, rsi, stoch = get_last_signals(df, window=10)
        assert macd == 2
        assert rsi == 0
        assert stoch == -2

    def test_window_equals_data(self):
        df = self._make_df([1, 2, 3], [4, 5, 6], [7, 8, 9])
        macd, rsi, stoch = get_last_signals(df, window=3)
        assert macd == 6
        assert rsi == 15
        assert stoch == 24

    def test_default_window(self):
        df = self._make_df([1] * 10, [0] * 10, [-1] * 10)
        macd, rsi, stoch = get_last_signals(df)
        assert macd == 5
        assert rsi == 0
        assert stoch == -5
