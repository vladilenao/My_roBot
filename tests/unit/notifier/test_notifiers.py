from unittest.mock import MagicMock, patch

import pytest

from src.notifier import (
    AbstractNotifier,
    ConsoleNotifier,
    DecisionFormatter,
    TelegramNotifier,
    get_notifier,
)
from src.strategies.contracts import Decision, SignalType


class RecordingNotifier(AbstractNotifier):
    def __init__(self, formatter=None):
        super().__init__(formatter)
        self.messages = []

    def notify(self, message):
        self.messages.append(message)


class TestAbstractNotifier:
    def test_abstract_notifier_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            AbstractNotifier()

    def test_subclass_without_notify_cannot_be_instantiated(self):
        class Incomplete(AbstractNotifier):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_default_formatter_is_decision_formatter(self):
        assert isinstance(RecordingNotifier()._formatter, DecisionFormatter)


class TestNotifyDecision:
    def test_formats_and_delivers_decision(self):
        notifier = RecordingNotifier()

        notifier.notify_decision(Decision(SignalType.BUY, 3.14159), "NG")

        assert notifier.messages == ["● NG ➜ 🟢 ПОКУПКА (BUY) — Цена: 3.142"]

    def test_custom_formatter_is_used(self):
        formatter = MagicMock()
        formatter.format.return_value = "custom text"
        notifier = RecordingNotifier(formatter=formatter)

        notifier.notify_decision(Decision(SignalType.HOLD, 10.0), "")

        assert notifier.messages == ["custom text"]
        formatter.format.assert_called_once_with(Decision(SignalType.HOLD, 10.0), "")


class TestConsoleNotifier:
    def test_notify_prints_message(self, capsys):
        ConsoleNotifier().notify("hello")

        assert capsys.readouterr().out == "hello\n"


class TestGetNotifier:
    def test_telegram_selected(self, monkeypatch):
        monkeypatch.setattr("src.notifier.NOTIFIER", "telegram")

        assert isinstance(get_notifier(), TelegramNotifier)

    def test_console_selected(self, monkeypatch):
        monkeypatch.setattr("src.notifier.NOTIFIER", "console")

        assert isinstance(get_notifier(), ConsoleNotifier)

    def test_unknown_channel_raises_with_available_list(self, monkeypatch):
        monkeypatch.setattr("src.notifier.NOTIFIER", "pigeon")

        with pytest.raises(ValueError, match="Доступны"):
            get_notifier()


class TestTelegramDefaults:
    def test_defaults_from_config(self, monkeypatch):
        monkeypatch.setattr("src.notifier.telegram.TELEGRAM_BOT_TOKEN", "cfg-token")
        monkeypatch.setattr("src.notifier.telegram.TELEGRAM_CHANNEL_ID", "cfg-chat")

        notifier = TelegramNotifier()

        assert notifier.bot_token == "cfg-token"
        assert notifier.channel_id == "cfg-chat"

    def test_explicit_params_override_config(self, monkeypatch):
        monkeypatch.setattr("src.notifier.telegram.TELEGRAM_BOT_TOKEN", "cfg-token")
        monkeypatch.setattr("src.notifier.telegram.TELEGRAM_CHANNEL_ID", "cfg-chat")

        notifier = TelegramNotifier(bot_token="own-token", channel_id="own-chat")

        assert notifier.bot_token == "own-token"
        assert notifier.channel_id == "own-chat"

    def test_empty_params_fall_back_to_config(self, monkeypatch):
        monkeypatch.setattr("src.notifier.telegram.TELEGRAM_BOT_TOKEN", "cfg-token")
        monkeypatch.setattr("src.notifier.telegram.TELEGRAM_CHANNEL_ID", "cfg-chat")

        notifier = TelegramNotifier(bot_token="", channel_id=None)

        assert notifier.bot_token == "cfg-token"
        assert notifier.channel_id == "cfg-chat"


class TestTelegramNotify:
    @patch("requests.post")
    def test_successful_send_uses_correct_request_without_echo(self, mock_post, capsys):
        mock_post.return_value.status_code = 200

        TelegramNotifier(bot_token="tok", channel_id="chat").notify("hi")

        mock_post.assert_called_once_with(
            "https://api.telegram.org/bottok/sendMessage",
            data={"chat_id": "chat", "text": "hi"},
            timeout=10,
        )
        assert capsys.readouterr().out == ""

    @patch("requests.post")
    def test_non_200_logs_error(self, mock_post, capsys):
        mock_post.return_value.status_code = 400
        mock_post.return_value.text = "Bad Request"

        TelegramNotifier(bot_token="tok", channel_id="chat").notify("hi")

        out = capsys.readouterr().out
        assert "Ошибка отправки в Telegram" in out
        assert "Bad Request" in out

    @patch("requests.post", side_effect=Exception("boom"))
    def test_network_exception_does_not_raise(self, mock_post, capsys):
        TelegramNotifier(bot_token="tok", channel_id="chat").notify("hi")

        assert "Исключение при отправке" in capsys.readouterr().out

    def test_missing_config_warns_without_request(self, monkeypatch, capsys):
        monkeypatch.setattr("src.notifier.telegram.TELEGRAM_BOT_TOKEN", None)
        monkeypatch.setattr("src.notifier.telegram.TELEGRAM_CHANNEL_ID", None)

        with patch("requests.post") as mock_post:
            TelegramNotifier().notify("hi")

        mock_post.assert_not_called()
        assert "не настроен" in capsys.readouterr().out
