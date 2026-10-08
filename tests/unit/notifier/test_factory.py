"""Unit-тесты фабрики каналов: сборка по конфигурации и закрытие."""

import pytest

from src.events import Event
from src.events.types import CONTROL_EVENT_TYPES, EventType
from src.notifier import ConsoleChannel, TelegramChannel, build_channels, close_channels


class _FakeChannel:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.closed = False
        self.fail = fail

    def close(self) -> None:
        if self.fail:
            raise RuntimeError("канал не закрылся")
        self.closed = True


class TestCloseChannels:
    def test_closes_every_channel(self) -> None:
        first, second = _FakeChannel(), _FakeChannel()

        close_channels([first, second])

        assert first.closed and second.closed

    def test_failure_of_one_channel_does_not_stop_others(self) -> None:
        broken, healthy = _FakeChannel(fail=True), _FakeChannel()

        close_channels([broken, healthy])

        assert healthy.closed is True


class TestBuildChannels:
    def _patch(self, monkeypatch, channels, console_events, telegram_events):
        monkeypatch.setattr("src.config.NOTIFIER_CHANNELS", channels)
        monkeypatch.setattr("src.config.NOTIFIER_CONSOLE_EVENTS", console_events)
        monkeypatch.setattr("src.config.NOTIFIER_TELEGRAM_EVENTS", telegram_events)

    def test_builds_console_channel_with_configured_events(self, monkeypatch) -> None:
        self._patch(monkeypatch, ("console",), ("decision", "signal"), ("signal",))

        (channel,) = build_channels()

        assert isinstance(channel, ConsoleChannel)
        assert channel.notification_types == frozenset({EventType.DECISION, EventType.SIGNAL})
        assert channel.supported_types == frozenset({EventType.DECISION, EventType.SIGNAL}) | CONTROL_EVENT_TYPES

    def test_builds_channels_in_configuration_order(self, monkeypatch) -> None:
        self._patch(monkeypatch, ("telegram", "console"), ("decision",), ("signal",))

        channels = build_channels()
        try:
            assert [type(channel) for channel in channels] == [TelegramChannel, ConsoleChannel]
        finally:
            close_channels(channels)

    def test_unknown_channel_raises(self, monkeypatch) -> None:
        self._patch(monkeypatch, ("sms",), ("decision",), ("signal",))

        with pytest.raises(ValueError, match="sms"):
            build_channels()

    def test_empty_channel_list_raises(self, monkeypatch) -> None:
        self._patch(monkeypatch, (), ("decision",), ("signal",))

        with pytest.raises(ValueError):
            build_channels()

    def test_telegram_without_credentials_is_built_but_disabled(self, monkeypatch) -> None:
        self._patch(monkeypatch, ("telegram",), ("decision",), ("signal",))
        monkeypatch.setattr("src.config.TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr("src.config.TELEGRAM_CHANNEL_ID", "")
        monkeypatch.setattr("src.config.CLOUDFLARE_URL", "")

        (channel,) = build_channels()

        assert channel.enabled is False

    def test_telegram_request_timeout_comes_from_config(self, monkeypatch) -> None:
        self._patch(monkeypatch, ("telegram",), ("decision",), ("signal",))
        monkeypatch.setattr("src.config.NOTIFIER_TELEGRAM_REQUEST_TIMEOUT", 7)

        (channel,) = build_channels()

        assert channel._transport.timeout == 7

    def test_telegram_max_transport_attempts_comes_from_config(self, monkeypatch) -> None:
        self._patch(monkeypatch, ("telegram",), ("decision",), ("signal",))
        monkeypatch.setattr("src.config.NOTIFIER_TELEGRAM_MAX_TRANSPORT_ATTEMPTS", 3)

        (channel,) = build_channels()

        assert channel._max_transport_attempts == 3

    def test_console_channel_prints_signal(self, monkeypatch, capsys) -> None:
        self._patch(monkeypatch, ("console",), ("signal",), ("signal",))
        (channel,) = build_channels()

        channel.handle(
            Event.signal("NG-10.26", side="BUY", quantity=1, entry=100, stop=96)
        )

        out = capsys.readouterr().out
        assert "NG-10.26" in out
        assert "в работе, ждёт подтверждения" in out
