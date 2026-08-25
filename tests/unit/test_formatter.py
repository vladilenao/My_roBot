from src.notifier import DecisionFormatter
from src.strategies.contracts import Decision, SignalType


class TestFormatDecision:
    def test_buy_without_label(self):
        result = DecisionFormatter().format(Decision(SignalType.BUY, 100.0))
        assert "ПОКУПАТЬ" in result
        assert "100.0" in result
        assert "[" not in result

    def test_sell_without_label(self):
        result = DecisionFormatter().format(Decision(SignalType.SELL, 50.0))
        assert "ПРОДАВАТЬ" in result
        assert "50.0" in result
        assert "[" not in result

    def test_hold_without_label(self):
        result = DecisionFormatter().format(Decision(SignalType.HOLD, 50.0))
        assert "Отдыхаем" in result
        assert "[" not in result

    def test_buy_with_label(self):
        result = DecisionFormatter().format(Decision(SignalType.BUY, 100.0), instrument_label="SBER share")
        assert result.startswith("[SBER share]")
        assert "ПОКУПАТЬ" in result
        assert "100.0" in result

    def test_sell_with_label(self):
        result = DecisionFormatter().format(Decision(SignalType.SELL, 50.0), instrument_label="NGU6 future")
        assert result.startswith("[NGU6 future]")
        assert "ПРОДАВАТЬ" in result

    def test_hold_with_label(self):
        result = DecisionFormatter().format(Decision(SignalType.HOLD, 50.0), instrument_label="GAZP share")
        assert result.startswith("[GAZP share]")
        assert "Отдыхаем" in result

    def test_price_rounding_buy(self):
        result = DecisionFormatter().format(Decision(SignalType.BUY, 3.14159))
        assert "3.142" in result

    def test_price_rounding_sell(self):
        result = DecisionFormatter().format(Decision(SignalType.SELL, 3.14159))
        assert "3.142" in result

    def test_empty_label_no_prefix(self):
        result = DecisionFormatter().format(Decision(SignalType.BUY, 100.0), instrument_label="")
        assert "[" not in result
        assert "ПОКУПАТЬ" in result

    def test_label_with_multiple_spaces_preserved(self):
        result = DecisionFormatter().format(Decision(SignalType.BUY, 100.0), instrument_label="SBER  share")
        assert result.startswith("[SBER  share]")

    def test_hold_has_no_price(self):
        result = DecisionFormatter().format(Decision(SignalType.HOLD, 3.14159), instrument_label="X")
        assert "Цена" not in result
