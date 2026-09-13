"""Преобразование исключений в пользовательские уведомления.

Технические дампы исключений пользователю не показываются: они остаются
в журнале ``bot_debug.log`` (там работает ``log.exception``). Отсюда же
берётся текст для Telegram-уведомления.

Преходящие ошибки (исчерпание лимита запросов к API) не уведомляют
пользователя отдельным сообщением — они учитываются счётчиком ошибок
в сердцебиении, а сам робот повторяет попытки с паузой.
"""

from __future__ import annotations

_RATE_LIMIT_MARKERS = (
    "RESOURCE_EXHAUSTED",
    "resource_exhausted",
)


def _is_rate_limit(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in _RATE_LIMIT_MARKERS)


def user_error_message(exc: Exception, operation: str) -> str | None:
    """Возвращает текст для пользователя или ``None``, если уведомлять не нужно.

    ``operation`` — человекочитаемое название операции, например
    «обновление данных SBER (15m)» или «анализ NG-9.26 (1h, flat_triangle)».
    """
    if _is_rate_limit(exc):
        return None
    return (
        f"❗ Сбой: {operation}. Робот продолжает работу. "
        "Подробности — в bot_debug.log рядом с роботом."
    )