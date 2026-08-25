import requests

from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID
from src.notifier.base import AbstractNotifier, DecisionFormatter


class TelegramNotifier(AbstractNotifier):
    """Отправляет сообщение в Telegram Bot API."""

    def __init__(
        self,
        bot_token: str | None = None,
        channel_id: str | None = None,
        formatter: DecisionFormatter | None = None,
    ) -> None:
        super().__init__(formatter)
        self.bot_token = bot_token if bot_token else TELEGRAM_BOT_TOKEN
        self.channel_id = channel_id if channel_id else TELEGRAM_CHANNEL_ID

    def notify(self, message: str) -> None:
        if not self.bot_token or not self.channel_id:
            print("⚠️ Telegram не настроен – сообщение не отправлено.")
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            r = requests.post(url, data={
                "chat_id": self.channel_id,
                "text": message
            }, timeout=10)
            if r.status_code != 200:
                print(f"Ошибка отправки в Telegram: {r.text}")
        except Exception as e:
            print(f"Исключение при отправке: {e}")
