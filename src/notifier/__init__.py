from src.config import NOTIFIER
from src.notifier.base import AbstractNotifier, DecisionFormatter
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
    return notifier_cls()
