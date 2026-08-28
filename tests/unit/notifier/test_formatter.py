import pandas as pd

from src.notifier import DecisionFormatter
from src.strategies.contracts import Decision, SignalType


def _decision(
    signal_type,
    price,
    *,
    bar_time=None,
    strategy_name=None,
):
    return Decision(signal_type, price, bar_time=bar_time, strategy_name=strategy_name)


class TestForwardFormat:
    def test_buy_with_bar_time_and_strategy(self):
        result = DecisionFormatter().format(
            _decision(
                SignalType.BUY,
                100.5,
                bar_time=pd.Timestamp("2026-08-26 22:00"),
                strategy_name="macd_rsi_stoch",
            ),
            instrument_label="NG-10.26 (1h)",
        )
        assert result == "● NG-10.26 (1h) 22:00 | macd_rsi_stoch ➜ 🟢 ПОКУПКА (BUY) — Цена: 100.5"

    def test_sell_with_bar_time_and_strategy(self):
        result = DecisionFormatter().format(
            _decision(
                SignalType.SELL,
                100.5,
                bar_time=pd.Timestamp("2026-08-26 22:00"),
                strategy_name="macd_rsi_stoch",
            ),
            instrument_label="NG-10.26 (1h)",
        )
        assert result == "● NG-10.26 (1h) 22:00 | macd_rsi_stoch ➜ 🔴 ПРОДАЖА (SELL) — Цена: 100.5"

    def test_hold_has_no_price(self):
        result = DecisionFormatter().format(_decision(SignalType.HOLD, 3.14159))
        assert "⏳ Нет сигнала." in result
        assert "Цена" not in result

    def test_omits_time_without_bar_time(self):
        result = DecisionFormatter().format(
            _decision(SignalType.BUY, 100.5, strategy_name="macd_rsi_stoch"),
            instrument_label="NG-10.26 (1h)",
        )
        assert "22:00" not in result
        assert "| macd_rsi_stoch" in result

    def test_omits_strategy_without_name(self):
        result = DecisionFormatter().format(
            _decision(SignalType.BUY, 100.5, bar_time=pd.Timestamp("2026-08-26 22:00")),
            instrument_label="NG-10.26 (1h)",
        )
        assert "22:00" in result
        assert "|" not in result


class TestFormatDecision:
    def test_line_always_starts_with_marker(self):
        result = DecisionFormatter().format(_decision(SignalType.BUY, 100.0))
        assert result.startswith("●")

    def test_emoji_for_buy(self):
        result = DecisionFormatter().format(_decision(SignalType.BUY, 100.0))
        assert "🟢 ПОКУПКА (BUY)" in result

    def test_emoji_for_sell(self):
        result = DecisionFormatter().format(_decision(SignalType.SELL, 50.0))
        assert "🔴 ПРОДАЖА (SELL)" in result

    def test_hold_is_hold(self):
        result = DecisionFormatter().format(_decision(SignalType.HOLD, 50.0))
        assert "⏳ Нет сигнала." in result

    def test_price_rounding_buy(self):
        result = DecisionFormatter().format(_decision(SignalType.BUY, 3.14159))
        assert "3.142" in result

    def test_price_rounding_sell(self):
        result = DecisionFormatter().format(_decision(SignalType.SELL, 3.14159))
        assert "3.142" in result


class TestTimeZoneOffset:
    def test_zero_offset_keeps_utc(self):
        result = DecisionFormatter(tz_offset_hours=0).format(
            _decision(
                SignalType.HOLD,
                1.0,
                bar_time=pd.Timestamp("2026-08-26 06:15"),
                strategy_name="macd_rsi_stoch",
            )
        )
        assert " 06:15 |" in result

    def test_positive_offset_shifts_to_local_time(self):
        result = DecisionFormatter(tz_offset_hours=3).format(
            _decision(
                SignalType.HOLD,
                1.0,
                bar_time=pd.Timestamp("2026-08-26 06:15"),
                strategy_name="macd_rsi_stoch",
            )
        )
        assert " 09:15 |" in result

    def test_negative_offset(self):
        result = DecisionFormatter(tz_offset_hours=-2).format(
            _decision(
                SignalType.HOLD,
                1.0,
                bar_time=pd.Timestamp("2026-08-26 06:15"),
                strategy_name="macd_rsi_stoch",
            )
        )
        assert " 04:15 |" in result

    def test_offset_does_not_mutate_decision_bar_time(self):
        decision = _decision(
            SignalType.HOLD,
            1.0,
            bar_time=pd.Timestamp("2026-08-26 06:15"),
            strategy_name="macd_rsi_stoch",
        )
        DecisionFormatter(tz_offset_hours=3).format(decision)
        assert decision.bar_time == pd.Timestamp("2026-08-26 06:15")

    def test_omits_time_without_bar_time_even_with_offset(self):
        result = DecisionFormatter(tz_offset_hours=3).format(
            _decision(
                SignalType.HOLD,
                1.0,
                strategy_name="macd_rsi_stoch",
            )
        )
        assert "06:15" not in result
        assert "09:15" not in result
        assert "| macd_rsi_stoch" in result
