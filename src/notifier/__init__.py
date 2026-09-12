from src.config import BAR_TIME_TZ_OFFSET_HOURS, NOTIFIER
from src.notifier.base import AbstractNotifier as AbstractNotifier
from src.notifier.base import DecisionFormatter
from src.notifier.console import ConsoleNotifier
from src.notifier.telegram import TelegramNotifier

_notifiers = {
    "telegram": TelegramNotifier,
    "console": ConsoleNotifier,
}


def get_notifier():
    notifier_cls = _notifiers.get(NOTIFIER)
    if notifier_cls is None:
        available = ", ".join(sorted(_notifiers))
        raise ValueError(f"Неизвестный канал уведомлений '{NOTIFIER}'. Доступны: {available}")
    formatter = DecisionFormatter(tz_offset_hours=BAR_TIME_TZ_OFFSET_HOURS)
    return notifier_cls(formatter=formatter)
