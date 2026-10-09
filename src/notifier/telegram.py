"""Telegram: очередь immutable Events, один render/IO worker, durable links."""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from typing import Iterable

from src.events.event import Event
from src.events.types import TRADING_EVENT_TYPES, EventType
from src.logging_setup import get_logger
from src.notifier.channel import Channel
from src.notifier.telegram_chart import build_scene, compact_png, render_png
from src.notifier.telegram_delivery import DeliveryRepository, markup, namespace
from src.notifier.telegram_html import split_html
from src.notifier.telegram_navigation import event_navigation, message_link
from src.notifier.telegram_templates import event_role, render
from src.notifier.telegram_transport import TelegramTransport

log = get_logger(__name__)
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
QUEUE_SIZE = 1000
CLOSE_TIMEOUT = 5.0
RETRY_DELAY = 2.0
FILE_CLEANUP_INTERVAL = 24 * 60 * 60


class TelegramChannel(Channel):
    name = "telegram"
    supported_types: frozenset[EventType] = TRADING_EVENT_TYPES

    def __init__(self, *, bot_token="", channel_id="", cloudflare_url="", tz_offset_hours=0.0,
                 supported_types: Iterable[EventType] | None = None, queue_size=QUEUE_SIZE,
                 request_timeout=10.0, max_transport_attempts=1, delivery_path=None):
        super().__init__(supported_types=supported_types)
        self.bot_token, self.channel_id, self.cloudflare_url = bot_token, channel_id, cloudflare_url
        self._tz_offset_hours = tz_offset_hours
        self._enabled = bool(bot_token and channel_id and cloudflare_url)
        self._closed, self._dropped = False, 0
        self._queue = queue.Queue(maxsize=queue_size)
        self._deadline = None
        self._delivery_path = delivery_path
        self._max_transport_attempts = max(1, int(max_transport_attempts))
        self._metadata_read, self._chat = False, None
        if not self._enabled:
            self._transport = None
            log.warning("Канал Telegram не настроен (нет токена, chat_id или адреса): уведомления не отправляются.")
            return
        self._transport = TelegramTransport(cloudflare_url, bot_token, channel_id, request_timeout)
        self._worker = threading.Thread(target=self._run, name="telegram-notifier", daemon=True)
        self._worker.start()

    @property
    def enabled(self):
        return self._enabled

    def handle(self, event: Event):
        if not self._closed and self._enabled and self.accepts(event):
            self._enqueue(event)

    def _enqueue(self, event):
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
            self._queue.task_done()
            self._dropped += 1
        except queue.Empty:
            pass
        if self._dropped == 1 or self._dropped % 100 == 0:
            log.warning("Очередь Telegram переполнена: отброшено %d %s, новые состояния сделки проходят.",
                        self._dropped, _plural(self._dropped, ("сообщение", "сообщения", "сообщений")))
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1

    def close(self, timeout=CLOSE_TIMEOUT):
        if self._closed:
            return
        self._deadline = time.monotonic() + timeout
        self._closed = True
        if self._enabled:
            self._worker.join(timeout)
            if self._worker.is_alive():
                log.warning("Поток Telegram не завершился за %.1f с: осталось %d сообщений.", timeout, self._queue.qsize())

    def _run(self):
        repository = None
        try:
            try:
                repository = DeliveryRepository(self._delivery_path, namespace(self.bot_token, self.channel_id, self._delivery_path))
                removed = repository.cleanup_files()
                if removed:
                    log.info("Telegram: удалено файлов старше 7 дней: %d.", removed)
            except Exception as exc:
                self._enabled = False
                log.warning("Telegram отключён: delivery-состояние недоступно (%s).", type(exc).__name__)
                return
            next_cleanup = time.monotonic() + FILE_CLEANUP_INTERVAL
            while True:
                if time.monotonic() >= next_cleanup:
                    try:
                        removed = repository.cleanup_files()
                        if removed:
                            log.info("Telegram: удалено файлов старше 7 дней: %d.", removed)
                    except Exception as exc:
                        log.warning("Telegram: очистка файлов не выполнена (%s).", type(exc).__name__)
                    next_cleanup = time.monotonic() + FILE_CLEANUP_INTERVAL
                if self._closed and (self._queue.empty() or time.monotonic() >= self._deadline):
                    dropped = 0
                    while True:
                        try:
                            self._queue.get_nowait()
                            self._queue.task_done()
                            dropped += 1
                        except queue.Empty:
                            break
                    if dropped:
                        log.warning("Telegram: при остановке отброшено %d сообщений.", dropped)
                    return
                try:
                    event = self._queue.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    if isinstance(event, str):
                        for chunk in _split_message(event):
                            self._transport.request("sendMessage", {"text": chunk})
                    else:
                        self._deliver(event, repository)
                except Exception as exc:
                    log.warning("Telegram: событие пропущено (%s); обработка продолжается.", type(exc).__name__)
                finally:
                    self._queue.task_done()
        finally:
            if repository is not None:
                repository.close()

    def _link(self, message_id):
        if not self._metadata_read:
            self._metadata_read = True
            response = self._transport.request("getChat")
            self._chat = response.result if response.ok and isinstance(response.result, dict) else None
        return message_link(self._chat, message_id)

    def _operation(self, repository, key, method, data, photo=None, *, trade_id="", event_key="", source="initial", navigation=None):
        """Сохранить операцию до HTTP и выполнить ограниченные попытки доставки."""
        operation_id = repository.save_operation(key, method, data, photo, trade_id=trade_id, event_key=event_key, navigation=navigation)
        if not repository.begin_operation(operation_id, source=source):
            # Ключ уже обработан (включая подтверждённый message_id) — не переотправляем.
            return None
        response = self._transport.request(method, data, photo)
        # begin_attempt вставил строку 'attempting': подтверждённого message_id у
        # ключа нет, поэтому неопределённый исход (таймаут/обрыв) повторяем —
        # не более max_transport_attempts попыток суммарно, только того же запроса.
        attempt = 1
        while response.retryable and attempt < self._max_transport_attempts:
            attempt += 1
            log.warning("Telegram %s: неопределённый исход, повтор %d из %d через %.1f с.",
                        method, attempt, self._max_transport_attempts, RETRY_DELAY)
            time.sleep(RETRY_DELAY)
            response = self._transport.request(method, data, photo)
        # В БД пишется только итог последней попытки; промежуточный uncertain
        # не фиксируется. ok=false/429 сюда не попадают: retryable у них False.
        repository.finish_operation(operation_id, response)
        return response

    def _deliver(self, event, repository):
        visual = event.get("visual")
        trade_id = event.get("trade_id") or (visual["trade_id"] if visual else "")
        event_key = visual["event_key"] if visual else event.get("execution_id") or event.get("event_id")
        if not event_key:
            event_key = hashlib.sha256(json.dumps(event.to_dict(), sort_keys=True).encode()).hexdigest()
        # Ownerless legacy messages have no cross-event sequence, but still an
        # operation key; no guessed trade association based on instrument names.
        sequence, revision = (visual["sequence"], visual["revision"]) if visual else (0, 0)
        if not repository.claim_event(trade_id, event_key, sequence, revision):
            return
        root = repository.root(trade_id) if trade_id else None
        buttons = []
        if root:
            link = self._link(root[0])
            if link:
                buttons.append({"text": "↗ Открыть сделку", "url": link})
        text = render(event, tz_offset_hours=self._tz_offset_hours)
        photo = None
        try:
            photo = render_png(build_scene(event, self._tz_offset_hours))
        except Exception as exc:
            log.warning("Telegram: график недоступен (%s), выбран текст.", type(exc).__name__)
        parts = split_html(text, CAPTION_LIMIT if photo else MESSAGE_LIMIT)
        key = f"{trade_id}:{event_key}"
        method = "sendPhoto" if photo else "sendMessage"
        data = {"caption" if photo else "text": parts[0], "parse_mode": "HTML", "reply_markup": markup(buttons)}
        if photo:
            try:
                original_size = len(photo)
                photo = compact_png(photo, self._transport.photo_budget(data))
                log.info("Telegram: график сжат %d → %d байт.", original_size, len(photo))
            except Exception as exc:
                log.warning("Telegram: сжатие графика недоступно (%s), выбран текст.", type(exc).__name__)
                photo = None
                parts = split_html(text, MESSAGE_LIMIT)
                method = "sendMessage"
                data = {"text": parts[0], "parse_mode": "HTML", "reply_markup": markup(buttons)}
        response = self._operation(repository, key + ":post", method, data, photo,
                                   trade_id=trade_id, event_key=event_key, navigation=event_navigation(event))
        if response is None or not response.ok:
            return
        message_id = response.result["message_id"]
        if event.type is EventType.SIGNAL and trade_id and root is None:
            repository.set_root(trade_id, message_id, "photo" if photo else "text", parts[0])
            root = repository.root(trade_id)
        elif trade_id:
            role = event_role(event)
            if role.startswith("ЦЕЛЬ") or role in {"СТОП", "Итог"}:
                repository.add_stage(trade_id, role, message_id)
            # Terminal target supplies both target and Итог navigation entries.
            if role == "Итог" and visual:
                fill = next((f for f in visual["fills"] if f["key"] == visual["event_key"]), None)
                if fill and fill["role"].startswith("ЦЕЛЬ"):
                    repository.add_stage(trade_id, fill["role"], message_id)
        continuation_buttons = buttons
        if root:
            link = self._link(root[0])
            continuation_buttons = [{"text": "↗ Открыть сделку", "url": link}] if link else []
        # Continuations are text-sized, rather than photo-caption-sized.
        for i, continuation in enumerate(split_html("".join(parts[1:])) if len(parts) > 1 else ()):
            self._operation(repository, f"{key}:detail:{i}", "sendMessage",
                            {"text": continuation, "parse_mode": "HTML", "reply_markup": markup(continuation_buttons)},
                            trade_id=trade_id, event_key=event_key)
        if root and visual and event.type is not EventType.SIGNAL:
            caption_parts = split_html(render(event, root=True, tz_offset_hours=self._tz_offset_hours),
                                       CAPTION_LIMIT if root[1] == "photo" else MESSAGE_LIMIT)
            root_buttons = []
            for role, stage_id in repository.stages(trade_id):
                link = self._link(stage_id)
                if link:
                    root_buttons.append({"text": role, "url": link})
            edit_method = "editMessageCaption" if root[1] == "photo" else "editMessageText"
            field = "caption" if root[1] == "photo" else "text"
            result = self._operation(repository, key + ":root", edit_method,
                                     {"message_id": root[0], field: caption_parts[0], "parse_mode": "HTML", "reply_markup": markup(root_buttons)},
                                     trade_id=trade_id, event_key=event_key)
            if result and result.ok:
                repository.update_root_text(trade_id, caption_parts[0])
                for i, detail in enumerate(split_html("".join(caption_parts[1:])) if len(caption_parts) > 1 else ()):
                    self._operation(repository, f"{key}:root-detail:{i}", "sendMessage",
                                    {"text": detail, "parse_mode": "HTML", "reply_markup": markup(continuation_buttons)},
                                    trade_id=trade_id, event_key=event_key)


def _plural(count, forms):
    if count % 10 == 1 and count % 100 != 11:
        return forms[0]
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return forms[1]
    return forms[2]


def _split_message(text, limit=MESSAGE_LIMIT):
    """Legacy plain-text helper; visual HTML uses the entity-aware splitter."""
    chunks, current = [], ""
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
    return chunks or [""]
