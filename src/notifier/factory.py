"""Фабрика каналов уведомлений.

Фабрика — единственное место, которое читает конфигурацию и секреты: сами
каналы получают только явные аргументы и не зависят от ``src.config``.
"""

from __future__ import annotations

from src.events.types import parse_event_types
from src.logging_setup import get_logger
from src.notifier.channel import Channel
from src.notifier.console import ConsoleChannel
from src.notifier.telegram import TelegramChannel

log = get_logger(__name__)


def build_channels() -> list[Channel]:
    """Собрать каналы, объявленные в конфигурации, в порядке конфигурации."""
    from src import config

    channels: list[Channel] = []
    for name in config.NOTIFIER_CHANNELS:
        if name == "console":
            events = config.NOTIFIER_CONSOLE_EVENTS
            channels.append(
                ConsoleChannel(
                    tz_offset_hours=config.BAR_TIME_TZ_OFFSET_HOURS,
                    supported_types=parse_event_types(events),
                )
            )
        elif name == "telegram":
            events = config.NOTIFIER_TELEGRAM_EVENTS
            channels.append(
                TelegramChannel(
                    bot_token=config.TELEGRAM_BOT_TOKEN,
                    channel_id=config.TELEGRAM_CHANNEL_ID,
                    cloudflare_url=config.CLOUDFLARE_URL,
                    tz_offset_hours=config.BAR_TIME_TZ_OFFSET_HOURS,
                    supported_types=parse_event_types(events),
                )
            )
        else:
            raise ValueError(
                f"Неизвестный канал уведомлений '{name}'. "
                f"Доступны: console, telegram"
            )
    if not channels:
        raise ValueError("Не выбран ни один канал уведомлений: укажите [notifier] channels")
    return channels


def close_channels(channels: list[Channel]) -> None:
    for channel in channels:
        try:
            channel.close()
        except Exception:
            log.exception("Канал %s не закрылся.", getattr(channel, "name", type(channel).__name__))
