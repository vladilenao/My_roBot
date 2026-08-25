import os
from dotenv import load_dotenv
from t_tech.invest import CandleInterval

from src.strategies.names import StrategyName

load_dotenv() # загружает переменные из .env

# Токены
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# Канал уведомлений: "telegram" | "console"
NOTIFIER = "console"

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

# Параметры бота (можно менять)
SLEEP_SECONDS = 9 # пауза между циклами (15 минут)
TIMEFRAME = "1h" # любой из ключей TIMEFRAMES

# Привязки инструментов к активным стратегиям (имена из реестра src.strategies)
# Акции и прочие нефьючерсные инструменты: ключ — точный тикер.
SHARE_STRATEGIES: dict[str, list[StrategyName]] = {
    "SBER": ["macd_rsi_stoch"],
}

# Фьючерсы: ключ — двухбуквенный код базового актива в верхнем регистре.
# Запись не привязана к конкретному контракту и действует на любой контракт актива
# (например "NG" покрывает NGU6, NGZ7 и любые последующие контракты природного газа).
FUTURE_STRATEGIES: dict[str, list[StrategyName]] = {
    "NG": ["macd_rsi_stoch"],
}

# Значения по умолчанию для fallback (тесты, одиночный запуск).
# При обычном запуске интерактивный выбор заменяет эти константы.
INSTRUMENT_TYPE = "future"
TICKER = "NGU6"