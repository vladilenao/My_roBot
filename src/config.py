import os
from typing import cast
from dotenv import load_dotenv
from t_tech.invest import CandleInterval

from src.config_loader import ConfigError, load_config
from src.strategies.contracts import DEFAULT_FILTER_PROFILE, Assignment
from src.strategies.names import StrategyName

load_dotenv()  # загружает переменные из .env

# Токены (секреты) — только из .env / переменных окружения, НЕ из robot.toml
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# Словарь таймфреймов (используется в загрузчике); всегда из кода
TIMEFRAMES = {
    "1m": CandleInterval.CANDLE_INTERVAL_1_MIN,
    "5m": CandleInterval.CANDLE_INTERVAL_5_MIN,
    "15m": CandleInterval.CANDLE_INTERVAL_15_MIN,
    "1h": CandleInterval.CANDLE_INTERVAL_HOUR,
    "1d": CandleInterval.CANDLE_INTERVAL_DAY,
    "1w": CandleInterval.CANDLE_INTERVAL_WEEK,
    "1M": CandleInterval.CANDLE_INTERVAL_MONTH,
}

# Смещение часового пояса (в часах) для отображения времени бара в уведомлениях.
# 0 = UTC (как хранится bar_time). Пользователь в МСК → 3.
BAR_TIME_TZ_OFFSET_HOURS = 3

_DEFAULTS = {
    "timeframe": "1h",
    "sleep_seconds": 3600,
    "heartbeat_every_ticks": 60,
    "tick_poll_secs": 1,
    "tick_timeout_secs": 65,
    "instrument_type": "future",
    "ticker": "NGU6",
    "notifier": "console",
    # Привязки инструментов к активным стратегиям (имена из реестра src.strategies)
    "share_strategies": {
        "SBER": {
            "strategies": [
                "macd_rsi_stoch",
                "flat_triangle",
                "harmonic_abcd",
                "ma_cloud_rsi_macd",
            ],
        },
    },
    "future_strategies": {
        "NG": {"strategies": ["macd_rsi_stoch", "flat_triangle", "harmonic_abcd", "ma_cloud_rsi_macd"]},
        "BR": {"strategies": ["macd_rsi_stoch", "flat_triangle", "harmonic_abcd", "ma_cloud_rsi_macd"]},
        "SI": {"strategies": ["macd_rsi_stoch", "flat_triangle", "harmonic_abcd", "ma_cloud_rsi_macd"]},
        "ED": {"strategies": ["macd_rsi_stoch", "flat_triangle", "harmonic_abcd", "ma_cloud_rsi_macd"]},
    },
    # Логирование (секция [logging] в robot.toml)
    "logging_service_uid": "b7e3a1c4-92f8-4d5e-a016-7f8b2c3d4e5a",
    "logging_file": "bot_debug.log",
    "logging_level": "DEBUG",
    "logging_max_bytes": 10_485_760,
    "logging_backup_count": 5,
}

_CONFIG = load_config(_DEFAULTS)

TIMEFRAME = _CONFIG["timeframe"]
if TIMEFRAME not in TIMEFRAMES:
    raise ConfigError(
        f"robot.toml: недопустимый таймфрейм {TIMEFRAME!r}; "
        f"допустимые: {', '.join(sorted(TIMEFRAMES))}"
    )

# Параметры бота (можно менять в robot.toml)
SLEEP_SECONDS = _CONFIG["sleep_seconds"]
HEARTBEAT_EVERY_TICKS = _CONFIG["heartbeat_every_ticks"]

# Ожидание свежего закрытого бара после границы закрытия свечи
# (у Tinkoff публикация бара происходит с задержкой до ~45+ сек).
TICK_POLL_SECS = _CONFIG["tick_poll_secs"]
TICK_TIMEOUT_SECS = _CONFIG["tick_timeout_secs"]

# Канал уведомлений: "telegram" | "console"
NOTIFIER = _CONFIG["notifier"]

def _checked_timeframe(value: str, where: str) -> str:
    """Таймфрейм привязки/тикера обязан быть ключом TIMEFRAMES (иначе ConfigError)."""
    if value not in TIMEFRAMES:
        raise ConfigError(
            f"robot.toml: недопустимый таймфрейм {value!r} ({where}); "
            f"допустимые: {', '.join(sorted(TIMEFRAMES))}"
        )
    return value


def _to_assignments(raw: dict[str, dict]) -> dict[str, list[Assignment]]:
    """Раскрытие гибридной записи привязок (строка | таблица) в Assignment.

    Каскад таймфрейма: `tf` инлайн-записи → `timeframe` таблицы тикера →
    глобальный TIMEFRAME раздела робота.
    """
    result: dict[str, list[Assignment]] = {}
    for ticker, table in raw.items():
        ticker_tf = table.get("timeframe")
        if ticker_tf is not None:
            _checked_timeframe(ticker_tf, f"timeframe тикера {ticker!r}")
        assignments: list[Assignment] = []
        for item in table["strategies"]:
            if isinstance(item, str):
                tf = ticker_tf or TIMEFRAME
                assignments.append(
                    Assignment(strategy=cast(StrategyName, item), timeframe=tf)
                )
            else:
                tf = item.get("tf") or ticker_tf or TIMEFRAME
                _checked_timeframe(tf, f"tf привязки {item['name']!r} для {ticker!r}")
                assignments.append(
                    Assignment(
                        strategy=cast(StrategyName, item["name"]),
                        filter_profile=item.get("filter", DEFAULT_FILTER_PROFILE),
                        timeframe=tf,
                    )
                )
        result[ticker] = assignments
    return result


SHARE_STRATEGIES: dict[str, list[Assignment]] = _to_assignments(_CONFIG["share_strategies"])

# Фьючерсы: ключ — двухбуквенный код базового актива в верхнем регистре.
# Запись не привязана к конкретному контракту и действует на любой контракт актива
# (например "NG" покрывает NGU6, NGZ7 и любые последующие контракты природного газа).
FUTURE_STRATEGIES: dict[str, list[Assignment]] = _to_assignments(_CONFIG["future_strategies"])

# Множество таймфреймов, задействованных привязками (ритм планировщика).
ACTIVE_TIMEFRAMES: frozenset[str] = frozenset(
    assignment.timeframe
    for items in (*SHARE_STRATEGIES.values(), *FUTURE_STRATEGIES.values())
    for assignment in items
)

# Значения по умолчанию для fallback (тесты, одиночный запуск).
# При обычном запуске интерактивный выбор заменяет эти константы.
INSTRUMENT_TYPE = _CONFIG["instrument_type"]
TICKER = _CONFIG["ticker"]

# Логирование
LOGGING_SERVICE_UID = _CONFIG["logging_service_uid"]
LOGGING_FILE = _CONFIG["logging_file"]
LOGGING_LEVEL = _CONFIG["logging_level"]
LOGGING_MAX_BYTES = _CONFIG["logging_max_bytes"]
LOGGING_BACKUP_COUNT = _CONFIG["logging_backup_count"]
