"""Настройка structured file-based логирования.

Весь технический вывод (отладка, rate-limit, индикаторы) пишется исключительно
в файл ``bot_debug.log`` через ``RotatingFileHandler``.  Консоль (stdout/stderr)
через логгер НЕ используется — UI-уведомления обрабатываются модулем ``notifier``.

Модуль предоставляет:
- ``setup_logging()`` — вызывается один раз в ``run.py`` перед ``TradingBot()``.
- ``get_logger(name)`` — возвращает ``ContextLoggerAdapter``, который подставляет
  ``service_uid`` и ``correlation_id`` из ``contextvars`` в каждую строку лога.
"""

from __future__ import annotations

import contextvars
import datetime
import logging
import logging.handlers
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

# ── contextvars ────────────────────────────────────────────────
service_uid_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "service_uid", default=""
)
correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


# ── ContextLoggerAdapter ──────────────────────────────────────
class ContextLoggerAdapter(logging.LoggerAdapter):
    """Подставляет ``service_uid`` и ``correlation_id`` в каждую запись."""

    def process(
        self, msg: str, kwargs: dict
    ) -> tuple[str, dict]:
        extra = kwargs.get("extra", {})
        extra.setdefault("service_uid", service_uid_var.get(""))
        extra.setdefault("correlation_id", correlation_id_var.get() or "")
        kwargs["extra"] = extra
        return msg, kwargs


# ── Handler filter ─────────────────────────────────────────────
class _ContextFilter(logging.Filter):
    """Встраивает ``service_uid`` и ``correlation_id`` в каждую запись.

    Записи, пришедшие от логгеров без адаптера (третьи библиотеки: t_tech,
    sentry_sdk и т.п.), не содержат этих полей, и массовый форматтер падает.
    Фильтр на хэндлере заполняет недостающие поля для любых источников.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service_uid"):
            record.service_uid = service_uid_var.get("")
        if not hasattr(record, "correlation_id"):
            record.correlation_id = correlation_id_var.get() or ""
        return True


# ── Secrets ────────────────────────────────────────────────────
REDACTED = "[скрыто]"
# Секрет короче этого не маскируется по значению: иначе пострадают обычные
# слова и числа. Короткие значения секретами по смыслу не бывают.
_MIN_SECRET_LENGTH = 8
# Форматы, которые маскируются, даже если значение не зарегистрировано:
# токен Telegram-бота (``123456:AA…``, в т.ч. внутри ``/bot123456:AA…/``)
# и значение заголовка ``Authorization: Bearer …``.
_SECRET_PATTERNS = (
    re.compile(r"(?<![0-9])[0-9]{6,}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/=-]+"),
)
_secrets: set[str] = set()


def register_secrets(values: Iterable[str | None]) -> None:
    """Запомнить значения секретов, которые не должны попасть в лог.

    Для адреса (``CLOUDFLARE_URL``) запоминается и его хост: сторонние
    библиотеки печатают хост отдельно от полного адреса.
    """
    for value in values:
        if not value:
            continue
        value = str(value).strip()
        candidates = {value, value.rstrip("/")}
        if "://" in value:
            host = urlsplit(value).hostname
            if host:
                candidates.add(host)
        _secrets.update(c for c in candidates if len(c) >= _MIN_SECRET_LENGTH)


def redact(text: str) -> str:
    """Вернуть текст, в котором секреты заменены на ``[скрыто]``."""
    # Длинные значения первыми: полный адрес раньше своего хоста.
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


# ── Formatter ──────────────────────────────────────────────────
class _MicrosecondFormatter(logging.Formatter):
    """Форматтер с реальными микросекундами в timestamp и маскированием секретов.

    Логирование использует ``time.strftime``, а на macOS ``%f`` в нём
    не поддерживается и выводится литеральной буквой «f». Поэтому время
    форматируется через ``datetime`` (даёт ``%Y-%m-%d %H:%M:%S.%f``).

    Маскируется уже готовая строка — сообщение, аргументы и traceback, —
    поэтому секрет не проходит и через записи сторонних библиотек
    (``urllib3`` печатает адрес запроса Telegram вместе с токеном).
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.datetime.fromtimestamp(record.created)
        if datefmt is not None:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


_DEFAULT_FORMAT = (
    "%(asctime)s [%(levelname)s] [%(service_uid)s] [tick:%(correlation_id)s] "
    "%(name)s (%(filename)s:%(lineno)d): %(message)s"
)
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S.%f"


def setup_logging(
    service_uid: str,
    log_file: str = "bot_debug.log",
    level: str = "DEBUG",
    max_bytes: int = 10_485_760,
    backup_count: int = 5,
    *,
    fmt: str = _DEFAULT_FORMAT,
    datefmt: str = _DEFAULT_DATEFMT,
    secrets: Iterable[str | None] = (),
) -> None:
    """Настройка ``logging`` на запись исключительно в файл.

    Вызывается **один раз** в ``run.py`` перед созданием ``TradingBot``.
    ``secrets`` — значения токенов, паролей и секретных адресов, которые
    заменяются на ``[скрыто]`` в каждой строке лога.
    """
    service_uid_var.set(service_uid)
    register_secrets(secrets)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))

    # Убираем все существующие хэндлеры (включая консольные)
    for h in list(root.handlers):
        root.removeHandler(h)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(_MicrosecondFormatter(fmt, datefmt=datefmt))
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)


# ── get_logger ────────────────────────────────────────────────
def get_logger(name: str) -> ContextLoggerAdapter:
    """Возвращает ``ContextLoggerAdapter`` для модуля ``name``."""
    return ContextLoggerAdapter(logging.getLogger(name))
