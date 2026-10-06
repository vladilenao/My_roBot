"""Канал Telegram: bounded-очередь и один поток отправки.

Рабочий поток робота публикует события синхронно, поэтому канал не ждёт сеть:
``handle()`` кладёт текст в ограниченную очередь и возвращается. Переполнение
означает, что сеть не успевает: тогда отбрасывается самое старое сообщение,
чтобы свежие состояния сделки не терялись.
"""

from __future__ import annotations

import queue
import threading
from typing import Iterable

import requests

from src.events.event import Event
from src.events.types import TRADING_EVENT_TYPES, EventType
from src.logging_setup import get_logger
from src.notifier.channel import Channel
from src.notifier.templates import render

log = get_logger(__name__)

MESSAGE_LIMIT = 4096
QUEUE_SIZE = 1000
CLOSE_TIMEOUT = 5.0

_SENTINEL = object()


class TelegramChannel(Channel):
    """Отправляет уведомления в Telegram Bot API."""

    name = "telegram"
    supported_types: frozenset[EventType] = TRADING_EVENT_TYPES

    def __init__(
        self,
        *,
        bot_token: str = "",
        channel_id: str = "",
        cloudflare_url: str = "",
        tz_offset_hours: float = 0.0,
        supported_types: Iterable[EventType] | None = None,
        queue_size: int = QUEUE_SIZE,
        request_timeout: float = 10.0,
    ) -> None:
        super().__init__(supported_types=supported_types)
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.cloudflare_url = cloudflare_url
        self._tz_offset_hours = tz_offset_hours
        self._request_timeout = request_timeout
        self._enabled = bool(bot_token and channel_id and cloudflare_url)
        self._closed = False
        self._dropped = 0
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        if not self._enabled:
            log.warning(
                "Канал Telegram не настроен (нет токена, chat_id или адреса): "
                "уведомления не отправляются, доставку обеспечивает консоль."
            )
            return
        self._worker = threading.Thread(
            target=self._run,
            name="telegram-notifier",
            daemon=True,
        )
        self._worker.start()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def handle(self, event: Event) -> None:
        if self._closed or not self._enabled or not self.accepts(event):
            return
        text = render(event, self._tz_offset_hours)
        if text is None:
            return
        self._enqueue(text)

    def close(self, timeout: float = CLOSE_TIMEOUT) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            log.warning(
                "Очередь Telegram переполнена на остановке: отброшено %d %s.",
                self._dropped,
                _plural(self._dropped, ("сообщение", "сообщения", "сообщений")),
            )
            return
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            log.warning(
                "Поток Telegram не завершился за %.1f с: осталось %d сообщений.",
                timeout,
                self._queue.qsize(),
            )

    def _enqueue(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        self._dropped += 1
        if self._dropped == 1 or self._dropped % 100 == 0:
            log.warning(
                "Очередь Telegram переполнена: отброшено %d %s, новые состояния сделки проходят.",
                self._dropped,
                _plural(self._dropped, ("сообщение", "сообщения", "сообщений")),
            )
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            for chunk in _split_message(item):
                self._send(chunk)

    def _send(self, text: str) -> None:
        url = f"{self.cloudflare_url}/bot{self.bot_token}/sendMessage"
        try:
            response = requests.post(
                url,
                data={"chat_id": self.channel_id, "text": text},
                timeout=self._request_timeout,
            )
        except Exception:
            log.exception("Исключение при отправке в Telegram: отправка пропущена.")
            return
        if response.status_code == 429:
            log.warning(
                "Telegram вернул 429, сообщение не повторяется (retry_after=%s).",
                _retry_after(response),
            )
            return
        if response.status_code != 200:
            log.warning("Ошибка отправки в Telegram: %s", response.text)


def _plural(count: int, forms: tuple[str, str, str]) -> str:
    """Согласовать существительное с числом: 1 сообщение, 2 сообщения, 5 сообщений."""
    if count % 10 == 1 and count % 100 != 11:
        return forms[0]
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return forms[1]
    return forms[2]


def _retry_after(response) -> int | None:
    """``retry_after`` из тела ответа Telegram, если бот его прислал."""
    try:
        parameters = response.json().get("parameters") or {}
    except Exception:
        return None
    value = parameters.get("retry_after")
    return value if isinstance(value, int) else None


def _split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Разрезать текст по границам строк, не превышая лимит Telegram."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks
