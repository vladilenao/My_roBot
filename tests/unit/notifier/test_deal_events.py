import pytest

from src.notifier.base import DealEventFormatter, DecisionFormatter


class StubNotifier:
    def __init__(self):
        self.messages = []

    def notify(self, message: str) -> None:
        self.messages.append(message)

    def notify_event(self, event_type: str, position_id: str, message: str) -> None:
        self.notify(DealEventFormatter.format_event(event_type, position_id, message))


class TestDealEventFormatter:
    @pytest.mark.parametrize(
        "event_type, expected_prefix",
        [
            ("order", "📝 Ордер"),
            ("fill", "💰 Сделка"),
            ("cancel", "❌ Отмена"),
            ("clear", "🏛 Клиринг"),
            ("over_risk", "⚠️ Over-risk"),
            ("protective", "🛡 Защита"),
            ("balance", "💼 Баланс"),
            ("unknown", "📌 Событие"),
        ],
    )
    def test_prefixes(self, event_type, expected_prefix):
        text = DealEventFormatter.format_event(event_type, "NG-1", "детали")
        assert text.startswith(expected_prefix)
        assert "детали" in text

    def test_position_id_not_required(self):
        text = DealEventFormatter.format_event("fill", "", "Вход BUY 2 NG")
        assert "Вход BUY 2 NG" in text


class TestNotifyEventWiring:
    def test_events_delivered_via_notifier(self):
        notifier = StubNotifier()
        notifier.notify_event("clear", "", "снимок баланса 100000 руб")
        notifier.notify_event("over_risk", "NG-1", "закрcovered контр-сделкой")
        assert len(notifier.messages) == 2
        assert notifier.messages[0].startswith("🏛 Клиринг")
        assert notifier.messages[1].startswith("⚠️ Over-risk")

    def test_decision_formatter_untouched(self):
        from src.strategies.contracts import Decision, SignalType

        fmt = DecisionFormatter()
        decision = Decision(
            signal_type=SignalType.BUY, price=100.5, timeframe="1h",
            strategy_name="ma_cloud_rsi_macd", indicator_values={},
            bar_time=None,
        )
        text = fmt.format(decision, "NG")
        assert "ПОКУПКА" in text and "ma_cloud_rsi_macd" in text