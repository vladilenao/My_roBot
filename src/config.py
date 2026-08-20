import os
from dotenv import load_dotenv
from t_tech.invest import CandleInterval

load_dotenv() # загружает переменные из .env

# Токены
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# Словарь таймфреймов (используется в загрузчике)
TIMEFRAMES = {
'1m': CandleInterval.CANDLE_INTERVAL_1_MIN,
'5m': CandleInterval.CANDLE_INTERVAL_5_MIN,
'15m': CandleInterval.CANDLE_INTERVAL_15_MIN,
'1h': CandleInterval.CANDLE_INTERVAL_HOUR,
'1d': CandleInterval.CANDLE_INTERVAL_DAY,
'1w': CandleInterval.CANDLE_INTERVAL_WEEK,
'1M': CandleInterval.CANDLE_INTERVAL_MONTH
}

# Параметры стратегии (можно менять)
SIGNAL_WINDOW = 5 # сколько последних свечей анализировать
SLEEP_SECONDS = 9 # пауза между циклами (15 минут)
TIMEFRAME = "1h" # любой из ключей TIMEFRAMES

# Значения по умолчанию для fallback (тесты, одиночный запуск).
# При обычном запуске интерактивный выбор取代 эти константы.
INSTRUMENT_TYPE = "future"
TICKER = "NGU6"