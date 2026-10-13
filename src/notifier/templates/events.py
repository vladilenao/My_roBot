"""Общие текстовые подписи диагностических событий для Telegram.

Импорт ``render`` сохранён для кода, использовавшего прежний путь шаблона
консоли; сам пользовательский формат живёт в ``console_view``.
"""

from src.notifier.templates.console_view import render as render

_DATA_HOLDS = {
    "BOOTSTRAPPING": "прогрев", "RECONCILING": "согласование с историей", "READY": "готово",
    "GAP_HOLD": "удержание: пропуск", "CONFLICT_HOLD": "удержание: расхождение версий",
    "INTEGRITY_HOLD": "удержание: изменена исполненная минута",
}

_NEXT_EVENT_LABELS = {
    "session_open": "открытие торгов", "session_close": "закрытие торгов",
    "break_start": "начало перерыва", "break_end": "конец перерыва",
    "bar_close": "закрытие свечи", "publication_deadline": "срок публикации свечи",
    "schedule_refresh": "обновление расписания",
}
_SCHEDULE_SOURCES = {"tbank": "брокер", "emulator": "снимок эмулятора"}


def next_event_label(kind) -> str:
    return _NEXT_EVENT_LABELS.get(kind, "событие")


def schedule_summary(schedule) -> str:
    """Источник и короткая версия расписания без технического кода режима."""
    if not schedule:
        return ""
    parts = [_SCHEDULE_SOURCES.get(schedule.get("source"), "источник неизвестен")]
    if schedule.get("digest"):
        parts.append(f"версия {schedule['digest']}")
    if schedule.get("snapshot_generation") is not None:
        parts.append(f"снимок {schedule['snapshot_generation']}")
    if schedule.get("stale"):
        parts.append("обновление не удалось")
    return " · ".join(parts)


def ingestion_summary(ingestion) -> str:
    """Одна строка о транспорте и готовности входа; пусто без среза owner."""
    if not ingestion:
        return ""
    transport = "поток" if ingestion.get("transport") == "stream" else "опрос"
    parts = [transport]
    if ingestion.get("transport") == "stream" and ingestion.get("epoch") is not None:
        parts.append(f"поколение {ingestion['epoch']}")
    if ingestion.get("fallback"):
        parts.append("резервный опрос вместо потока")
    hold = ingestion.get("data_hold")
    if hold:
        parts.append(_DATA_HOLDS.get(hold, hold))
    if ingestion.get("pre_cutover_gaps"):
        parts.append(f"пробелов до запуска без подтверждения: {ingestion['pre_cutover_gaps']}")
    return "Источник данных: " + " · ".join(parts)
