from unittest.mock import patch

import pytest

from src.events.bus import EventBus
from src.events.event import Event
from src.events.types import EventType
from src.notifier.factory import build_channels, close_channels


class _SpyChannel:
    name = "spy"

    def __init__(self) -> None:
        self.seen = []
        self.closed = False
        self.supported_types = {EventType.TRADE_CLOSED}

    def handle(self, event) -> None:
        self.seen.append(event)

    def close(self) -> None:
        self.closed = True


def _event() -> Event:
    return Event(type=EventType.TRADE_CLOSED, payload={})


class TestEmptyChannels:
    def test_build_returns_empty_list(self):
        assert build_channels([]) == []

    def test_build_ignores_configured_channels(self):
        with patch("src.config.NOTIFIER_CHANNELS", ["console", "telegram"]):
            assert build_channels([]) == []

    def test_configured_channels_still_built_by_default(self):
        with patch("src.config.NOTIFIER_CHANNELS", ["console"]):
            channels = build_channels()

        assert [channel.name for channel in channels] == ["console"]

    def test_unknown_channel_name_rejected(self):
        with pytest.raises(ValueError):
            build_channels(["sms"])

    def test_subscribe_all_of_empty_list_is_noop(self):
        bus = EventBus()
        channel = _SpyChannel()

        bus.subscribe_all([])

        bus.publish(_event())
        assert channel.seen == []

    def test_close_of_empty_list_is_noop(self):
        close_channels([])

    def test_no_notifications_delivered_in_history(self):
        channels = build_channels([])
        bus = EventBus()
        bus.subscribe_all(channels)

        bus.publish(_event())

        assert channels == []
        close_channels(channels)
        assert bus._subscribers == []

    def test_subscribe_all_still_subscribes_given_channels(self):
        bus = EventBus()
        channel = _SpyChannel()

        bus.subscribe_all([channel])
        bus.publish(_event())

        assert len(channel.seen) == 1
        close_channels([channel])
        assert channel.closed is True
