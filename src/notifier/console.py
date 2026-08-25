from src.notifier.base import AbstractNotifier


class ConsoleNotifier(AbstractNotifier):
    """Печатает сообщение в stdout."""

    def notify(self, message: str) -> None:
        print(message)
