"""Telegram: очередь immutable Events, один render/IO worker, durable links."""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Iterable

from src.events.event import Event
from src.events.types import TRADING_EVENT_TYPES, EventType
from src.logging_setup import get_logger
from src.notifier.channel import Channel
from src.notifier.telegram_chart import build_scene, compact_png, render_png
from src.notifier.telegram_delivery import DeliveryRepository, markup, namespace
from src.notifier.telegram_html import split_html
from src.notifier.telegram_navigation import event_navigation, message_link, register_confirmed
from src.notifier.telegram_templates import render
from src.notifier.templates.orders import order_line
from src.notifier.telegram_transport import ApiResult, TelegramTransport

log = get_logger(__name__)
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
QUEUE_SIZE = 1000
CLOSE_TIMEOUT = 5.0
RETRY_DELAY = 2.0
FILE_CLEANUP_INTERVAL = 24 * 60 * 60
CHAT_RETRY_DELAY = 60.0


class _Deferred(Exception):
    """Запрос операции не ушёл: событие ждёт фоновой досылки к ``due`` (UTC)."""

    def __init__(self, due):
        super().__init__()
        self.due = due


@dataclass
class _Item:
    """Событие в очереди ожидания сделки или операция из базы после перезапуска."""

    event: Event | None = None
    operation_id: int | None = None
    started: bool = False
    due: datetime | None = None


class TelegramChannel(Channel):
    name = "telegram"
    supported_types: frozenset[EventType] = TRADING_EVENT_TYPES

    def __init__(self, *, bot_token="", channel_id="", cloudflare_url="", tz_offset_hours=0.0,
                 supported_types: Iterable[EventType] | None = None, queue_size=QUEUE_SIZE,
                 request_timeout=10.0, max_transport_attempts=1, delivery_path=None, title=None):
        super().__init__(supported_types=supported_types)
        # Метка контура в заголовке карточки (add-tinvest-orders): «ПЕСОЧНИЦА», «СЧЁТ».
        self._title = title
        self.bot_token, self.channel_id, self.cloudflare_url = bot_token, channel_id, cloudflare_url
        self._tz_offset_hours = tz_offset_hours
        self._enabled = bool(bot_token and channel_id and cloudflare_url)
        self._closed, self._dropped = False, 0
        self._queue = queue.Queue(maxsize=queue_size)
        self._deadline = None
        self._delivery_path = delivery_path
        self._max_transport_attempts = max(1, int(max_transport_attempts))
        self._metadata_read, self._chat, self._chat_retry_at = False, None, 0.0
        # Очереди ожидания сделок с отложенной операцией: ключ → {"items", "due"}.
        self._gates: dict[str, dict] = {}
        self._parking = False
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
            self._restore(repository)
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
                    self._park(repository)
                    return
                if not self._closed:
                    self._retry_due(repository)
                try:
                    event = self._queue.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    if isinstance(event, str):
                        for chunk in _split_message(event):
                            self._transport.request("sendMessage", {"text": chunk})
                    else:
                        self._dispatch(event, repository)
                except Exception as exc:
                    log.warning("Telegram: событие пропущено (%s); обработка продолжается.", type(exc).__name__)
                finally:
                    self._queue.task_done()
        finally:
            if repository is not None:
                repository.close()

    def _gate_key(self, item):
        if item.event is not None:
            visual = item.event.get("visual")
            trade_id = item.event.get("trade_id") or (visual["trade_id"] if visual else "")
        else:
            trade_id = ""
        # Сообщения без сделки ни с чем не упорядочиваются: у каждого свой ключ.
        return f"trade:{trade_id}" if trade_id else f"item:{id(item)}"

    def _dispatch(self, event, repository):
        """Событие сделки с отложенной операцией ждёт за ней, остальные идут сразу."""
        item = _Item(event)
        key = self._gate_key(item)
        if key in self._gates:
            self._gates[key]["items"].append(item)
            return
        if not self._attempt(key, item, repository):
            self._gates[key] = {"items": deque([item]), "due": item.due}

    def _attempt(self, key, item, repository):
        """Выполнить элемент; False — он отложен и остаётся головой очереди сделки."""
        try:
            if item.event is None:
                self._deliver_stored(item.operation_id, repository)
            else:
                self._deliver(item.event, repository, resume=item.started)
        except _Deferred as deferred:
            item.started, item.due = True, deferred.due
            return False
        except Exception as exc:
            log.warning("Telegram: событие пропущено (%s); обработка продолжается.", type(exc).__name__)
        return True

    def _drain(self, key, repository):
        gate = self._gates[key]
        while gate["items"]:
            item = gate["items"][0]
            if not self._attempt(key, item, repository):
                gate["due"] = item.due
                return
            gate["items"].popleft()
        del self._gates[key]

    def _retry_due(self, repository):
        if not self._gates:
            return
        now = datetime.now(timezone.utc)
        for key in [key for key, gate in self._gates.items() if gate["due"] <= now]:
            self._drain(key, repository)

    def _restore(self, repository):
        """Подхватить отложенные операции прежнего запуска до приёма новых событий."""
        try:
            rows = repository.deferred_operations()
        except Exception as exc:
            log.warning("Telegram: отложенные сообщения не прочитаны (%s).", type(exc).__name__)
            return
        for row in rows:
            item = _Item(operation_id=row["operation_id"], started=True)
            key = f"trade:{row['trade_id']}" if row["trade_id"] else self._gate_key(item)
            gate = self._gates.setdefault(key, {"items": deque(), "due": row["due"]})
            gate["items"].append(item)
            gate["due"] = min(gate["due"], row["due"])
        if rows:
            log.info("Telegram: подхвачено отложенных сообщений прежнего запуска: %d.", len(rows))

    def _park(self, repository):
        """При остановке сохранить ожидающие события как отложенные операции без HTTP."""
        parked = 0
        self._parking = True
        try:
            for gate in self._gates.values():
                for item in gate["items"]:
                    if item.started or item.event is None:
                        continue
                    try:
                        self._deliver(item.event, repository)
                    except _Deferred:
                        parked += 1
                    except Exception as exc:
                        log.warning("Telegram: событие не сохранено для досылки (%s).", type(exc).__name__)
        finally:
            self._parking = False
        if parked:
            log.info("Telegram: сохранено для досылки после перезапуска: %d.", parked)

    def _deliver_stored(self, operation_id, repository):
        """Досылка операции прежнего запуска из сохранённых материалов."""
        material = repository.operation_material(operation_id)
        if material is None:
            return
        response = self._operation(repository, material["operation_key"], material["method"], material["data"],
                                   material.get("photo"), trade_id=material["trade_id"], event_key=material["event_key"])
        if response is not None and response.ok and material["trade_id"]:
            # Корень и роли этапа — из сохранённой навигации, без HTTP.
            register_confirmed(repository, material["trade_id"])

    def _link(self, message_id):
        if not self._metadata_read and not self._parking and time.monotonic() >= self._chat_retry_at:
            response = self._transport.request("getChat")
            if response.ok and isinstance(response.result, dict):
                self._chat, self._metadata_read = response.result, True
            elif response.retryable:
                # Сетевой сбой не отключает ссылки до конца запуска.
                self._chat_retry_at = time.monotonic() + CHAT_RETRY_DELAY
            else:
                self._metadata_read = True
        return message_link(self._chat, message_id)

    def _operation(self, repository, key, method, data, photo=None, *, trade_id="", event_key="", source="initial", navigation=None):
        """Сохранить операцию до HTTP и выполнить ограниченные попытки доставки."""
        existing = repository.operation(key)
        if existing is None:
            operation_id = repository.save_operation(key, method, data, photo, trade_id=trade_id, event_key=event_key, navigation=navigation)
            status = "pending"
        else:
            operation_id, status = existing["operation_id"], existing["status"]
        if status == "confirmed":
            # Продолжение отложенного события: шаг уже выполнен, повтор не нужен.
            return ApiResult(True, {"message_id": existing["message_id"]} if existing["message_id"] else None)
        if status not in {"pending", "deferred"}:
            # uncertain/failed/superseded автоматически не отправляются.
            return None
        if existing is not None:
            # Досылка — только исходные материалы, без перерисовки текста и PNG.
            material = repository.operation_material(operation_id)
            if material.get("media_error"):
                repository.abandon_operation(operation_id, "media-unavailable")
                return None
            method, data, photo = material["method"], material["data"], material.get("photo")
            source = "automatic"
        if self._parking:
            due = repository.defer_operation(operation_id, attempted=False)
            if due is None:
                return None
            raise _Deferred(due)
        if not repository.begin_operation(operation_id, source=source):
            return None
        response = self._transport.request(method, data, photo)
        # begin_attempt вставил строку 'attempting': подтверждённого message_id у
        # ключа нет, поэтому транспортный сбой повторяем — не более
        # max_transport_attempts попыток суммарно, только того же запроса.
        uncertain = response.uncertain
        attempt = 1
        while response.retryable and attempt < self._max_transport_attempts:
            attempt += 1
            log.warning("Telegram %s: сбой транспорта (%s), повтор %d из %d через %.1f с.",
                        method, response.detail, attempt, self._max_transport_attempts, RETRY_DELAY)
            time.sleep(RETRY_DELAY)
            response = self._transport.request(method, data, photo)
            uncertain = uncertain or response.uncertain
        if response.unsent and not uncertain:
            # Ни одна попытка не ушла: Telegram сообщения не получал, дубль невозможен.
            due = repository.defer_operation(operation_id, response.detail)
            if due is not None:
                log.warning("Telegram %s: запрос не ушёл (%s), фоновая досылка через %.0f с.",
                            method, response.detail, max(0.0, (due - datetime.now(timezone.utc)).total_seconds()))
                raise _Deferred(due)
            log.warning("Telegram %s: окно досылки истекло, сообщение доступно --telegram-retry.", method)
            return response
        if response.unsent:
            # Одна из попыток могла дойти: итог неопределённый, автоматически не досылается.
            response = ApiResult(False, uncertain=True, detail=response.detail)
        # В БД пишется только итог последней попытки; промежуточный uncertain
        # не фиксируется. ok=false/429 сюда не попадают: retryable у них False.
        repository.finish_operation(operation_id, response)
        return response

    def _titled(self, event, text):
        """Метка контура в первой строке карточки; у событий контура на брокере она уже есть."""
        if not self._title or event.type in {EventType.BROKER_ACCOUNT, EventType.BROKER_ORDER}:
            return text
        if text.startswith("<b>"):
            return f"<b>{escape(self._title)} · " + text[3:]
        return f"<b>{escape(self._title)}</b> · " + text

    def _note(self, event, root, repository, key):
        """Строка жизни заявки дописывается в корневую карточку сделки правкой сообщения."""
        trade_id = event.get("trade_id")
        stamp = ""
        if event.bar_time is not None:
            moment = event.bar_time if event.bar_time.tzinfo else event.bar_time.replace(tzinfo=timezone.utc)
            stamp = moment.astimezone(timezone(timedelta(hours=self._tz_offset_hours))).strftime("%H:%M:%S") + " "
        line = escape(stamp + order_line(event))
        if line not in repository.notes(trade_id):
            repository.add_note(trade_id, line)
        limit = CAPTION_LIMIT if root[1] == "photo" else MESSAGE_LIMIT
        buttons = [{"text": role, "url": link} for role, stage_id in repository.stages(trade_id)
                   if (link := self._link(stage_id))]
        method = "editMessageCaption" if root[1] == "photo" else "editMessageText"
        field = "caption" if root[1] == "photo" else "text"
        self._operation(repository, key + ":note", method,
                        {"message_id": root[0], field: _with_notes(root[2] or "", repository.notes(trade_id), limit),
                         "parse_mode": "HTML", "reply_markup": markup(buttons)},
                        trade_id=trade_id, event_key=key)

    def _deliver(self, event, repository, resume=False):
        visual = event.get("visual")
        trade_id = event.get("trade_id") or (visual["trade_id"] if visual else "")
        event_key = visual["event_key"] if visual else event.get("execution_id") or event.get("event_id")
        if not event_key:
            event_key = hashlib.sha256(json.dumps(event.to_dict(), sort_keys=True).encode()).hexdigest()
        # Ownerless legacy messages have no cross-event sequence, but still an
        # operation key; no guessed trade association based on instrument names.
        sequence, revision = (visual["sequence"], visual["revision"]) if visual else (0, 0)
        if not resume and not repository.claim_event(trade_id, event_key, sequence, revision):
            return
        root = repository.root(trade_id) if trade_id else None
        if root and event.type in _ROOT_NOTE_TYPES:
            self._note(event, root, repository, f"{trade_id}:{event_key}")
            return
        buttons = []
        if root:
            link = self._link(root[0])
            if link:
                buttons.append({"text": "↗ Открыть сделку", "url": link})
        text = self._titled(event, render(event, tz_offset_hours=self._tz_offset_hours))
        key = f"{trade_id}:{event_key}"
        # Продолжение отложенного события: основной пост уже сохранён, график не
        # перерисовывается — _operation возьмёт сохранённые материалы.
        stored = repository.operation(key + ":post") if resume else None
        photo = None
        if stored is None:
            try:
                photo = render_png(build_scene(event, self._tz_offset_hours))
            except Exception as exc:
                log.warning("Telegram: график недоступен (%s), выбран текст.", type(exc).__name__)
        parts = split_html(text, CAPTION_LIMIT if photo else MESSAGE_LIMIT)
        method = stored["method"] if stored is not None else "sendPhoto" if photo else "sendMessage"
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
        navigation = event_navigation(event)
        response = self._operation(repository, key + ":post", method, data, photo,
                                   trade_id=trade_id, event_key=event_key, navigation=navigation)
        if response is None or not response.ok:
            return
        message_id = response.result["message_id"]
        # Вид поста — по сохранённой операции: при продолжении график не рисовался.
        photo_post = method == "sendPhoto"
        parts = split_html(text, CAPTION_LIMIT if photo_post else MESSAGE_LIMIT)
        if event.type is EventType.SIGNAL and trade_id and root is None:
            repository.set_root(trade_id, message_id, "photo" if photo_post else "text", parts[0])
            root = repository.root(trade_id)
        elif trade_id:
            # Роли этапа — те же, что сохранены в навигацию до HTTP.
            for role in navigation["roles"]:
                repository.add_stage(trade_id, role, message_id)
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
            limit = CAPTION_LIMIT if root[1] == "photo" else MESSAGE_LIMIT
            caption_parts = split_html(self._titled(event, render(event, root=True, tz_offset_hours=self._tz_offset_hours)),
                                       limit)
            noted = _with_notes(caption_parts[0], repository.notes(trade_id), limit)
            root_buttons = []
            for role, stage_id in repository.stages(trade_id):
                link = self._link(stage_id)
                if link:
                    root_buttons.append({"text": role, "url": link})
            edit_method = "editMessageCaption" if root[1] == "photo" else "editMessageText"
            field = "caption" if root[1] == "photo" else "text"
            result = self._operation(repository, key + ":root", edit_method,
                                     {"message_id": root[0], field: noted, "parse_mode": "HTML", "reply_markup": markup(root_buttons)},
                                     trade_id=trade_id, event_key=event_key)
            if result and result.ok:
                repository.update_root_text(trade_id, caption_parts[0])
                for i, detail in enumerate(split_html("".join(caption_parts[1:])) if len(caption_parts) > 1 else ()):
                    self._operation(repository, f"{key}:root-detail:{i}", "sendMessage",
                                    {"text": detail, "parse_mode": "HTML", "reply_markup": markup(continuation_buttons)},
                                    trade_id=trade_id, event_key=event_key)


_ROOT_NOTE_TYPES = frozenset({EventType.BROKER_ORDER, EventType.ORDER_ACCEPTED})


def _with_notes(text, notes, limit):
    """Корневая карточка и строки жизни заявок под ней — сколько помещается в лимит."""
    for line in notes:
        candidate = f"{text}\n{line}"
        if len(candidate) > limit:
            break
        text = candidate
    return text


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
