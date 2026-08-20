import requests
from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID


def send_signal(text):


    """
    Отправляет сообщение получателю (сейчас – в Telegram).
    Вы можете переписать эту функцию, чтобы отправлять куда угодно.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        print("⚠️ Telegram не настроен – сообщение не отправлено.")
    print(text)
    return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "text": text
        }, timeout=10)
        if r.status_code != 200:
            print(f"Ошибка отправки в Telegram: {r.text}")
    except Exception as e:
        print(f"Исключение при отправке: {e}")
