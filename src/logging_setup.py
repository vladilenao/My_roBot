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
from pathlib import Path

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


# ── Formatter ──────────────────────────────────────────────────
class _MicrosecondFormatter(logging.Formatter):
    """Форматтер с реальными микросекундами в timestamp.

    Логирование использует ``time.strftime``, а на macOS ``%f`` в нём
    не поддерживается и выводится литеральной буквой «f». Поэтому время
    форматируется через ``datetime`` (даёт ``%Y-%m-%d %H:%M:%S.%f``).
    """

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
) -> None:
    """Настройка ``logging`` на запись исключительно в файл.

    Вызывается **один раз** в ``run.py`` перед созданием ``TradingBot``.
    """
    service_uid_var.set(service_uid)

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
