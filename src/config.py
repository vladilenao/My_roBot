import os
from pathlib import Path
from typing import cast
from dotenv import load_dotenv
from t_tech.invest import CandleInterval

from src.config_loader import (
    ConfigError,
    app_dir,
    derived_positions_file,
    load_config,
    validate_triple_screen_hierarchy,
)
from src.decision.filters.triple_screen import TripleScreenParams
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
    "30m": CandleInterval.CANDLE_INTERVAL_30_MIN,
    "1h": CandleInterval.CANDLE_INTERVAL_HOUR,
    "4h": CandleInterval.CANDLE_INTERVAL_4_HOUR,
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
                {"id": "share-sber-macd", "name": "macd_rsi_stoch", "management": "levels_rr"},
                {"id": "share-sber-flat", "name": "flat_triangle", "management": "levels_rr"},
                {"id": "share-sber-harmonic", "name": "harmonic_abcd", "management": "pattern_targets"},
                {"id": "share-sber-ma", "name": "ma_cloud_rsi_macd", "management": "ma_cloud"},
            ],
        },
    },
    "future_strategies": {
        "NG": {"strategies": [{"id": "future-ng-macd", "name": "macd_rsi_stoch", "management": "levels_rr"}]},
        "BR": {"strategies": [{"id": "future-br-macd", "name": "macd_rsi_stoch", "management": "levels_rr"}]},
        "SI": {"strategies": [{"id": "future-si-macd", "name": "macd_rsi_stoch", "management": "levels_rr"}]},
        "ED": {"strategies": [{"id": "future-ed-macd", "name": "macd_rsi_stoch", "management": "levels_rr"}]},
    },
    # Логирование (секция [logging] в robot.toml)
    "logging_service_uid": "b7e3a1c4-92f8-4d5e-a016-7f8b2c3d4e5a",
    "logging_file": "bot_debug.log",
    "logging_level": "DEBUG",
    "logging_max_bytes": 10_485_760,
    "logging_backup_count": 5,
    "audit_file": "trade_audit.log",
    "audit_max_bytes": 10_485_760,
    "audit_backup_count": 5,
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
    """Раскрытие явной записи привязок в Assignment.

    Каскад таймфрейма: `tf` инлайн-записи → `timeframe` таблицы тикера →
    глобальный TIMEFRAME раздела робота.
    """
    result: dict[str, list[Assignment]] = {}
    assignment_ids: set[str] = set()
    for ticker, table in raw.items():
        ticker_tf = table.get("timeframe")
        if ticker_tf is not None:
            _checked_timeframe(ticker_tf, f"timeframe тикера {ticker!r}")
        assignments: list[Assignment] = []
        for item in table["strategies"]:
            if isinstance(item, str):
                raise ConfigError(f"robot.toml: привязка для {ticker!r} должна содержать id и management")
            assignment_id = item.get("id")
            management = item.get("management")
            if not isinstance(assignment_id, str) or not assignment_id:
                raise ConfigError(f"robot.toml: у привязки {item.get('name')!r} для {ticker!r} отсутствует непустой id")
            if assignment_id in assignment_ids:
                raise ConfigError(f"robot.toml: повторяющийся id привязки {assignment_id!r}")
            if not isinstance(management, str) or not management:
                raise ConfigError(f"robot.toml: у привязки {assignment_id!r} отсутствует management")
            assignment_ids.add(assignment_id)
            tf = item.get("tf") or ticker_tf or TIMEFRAME
            _checked_timeframe(tf, f"tf привязки {item['name']!r} для {ticker!r}")
            assignments.append(
                Assignment(
                    id=assignment_id,
                    strategy=cast(StrategyName, item["name"]),
                    management=management,
                    filter_profile=item.get("filter", DEFAULT_FILTER_PROFILE),
                    priority=item.get("priority", 0),
                    timeframe=tf,
                )
            )
        result[ticker] = assignments
    return result


def _validate_global_assignment_ids(*sources: dict[str, list[Assignment]]) -> None:
    assignment_ids = [
        assignment.id
        for source in sources
        for assignments in source.values()
        for assignment in assignments
    ]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise ConfigError("robot.toml: id привязки должен быть глобально уникальным")


SHARE_STRATEGIES: dict[str, list[Assignment]] = _to_assignments(_CONFIG["share_strategies"])

# Фьючерсы: ключ — двухбуквенный код базового актива в верхнем регистре.
# Запись не привязана к конкретному контракту и действует на любой контракт актива
# (например "NG" покрывает NGU6, NGZ7 и любые последующие контракты природного газа).
FUTURE_STRATEGIES: dict[str, list[Assignment]] = _to_assignments(_CONFIG["future_strategies"])

_validate_global_assignment_ids(SHARE_STRATEGIES, FUTURE_STRATEGIES)

# Параметры профиля triple_screen (методика Элдера) из [strategies.filter.triple_screen];
# при отсутствии секции — дефолты (множитель 5, MACD 12/26/9, Stochastic 14/3/3, 20/80).
TRIPLE_SCREEN_PARAMS: TripleScreenParams = TripleScreenParams.from_config(
    _CONFIG.get("triple_screen_params") or {}
)

# Fail-fast на недопустимую иерархию ТФ: шаг множителя не должен выходить за лестницу TIMEFRAMES.
validate_triple_screen_hierarchy(
    {**SHARE_STRATEGIES, **FUTURE_STRATEGIES},
    TRIPLE_SCREEN_PARAMS.multiplier,
    tuple(TIMEFRAMES),
    app_dir() / "robot.toml",
)

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

# Каталог runtime-агрегатов (БД, CSV-проекции, аудит- и debug-лог).
DATA_DIR = _CONFIG.get("data_dir", "data")


def runtime_dir() -> Path:
    """Абсолютный каталог состояния робота: ``app_dir()/data_dir``.

    В dev это ``<корень проекта>/data``, в PyInstaller-сборке — ``data`` рядом
    с исполняемым файлом. Каталог не создаётся здесь: создание выполняет тот,
    кто открывает первые файлы (``run.main``).
    """
    path = Path(DATA_DIR)
    return path if path.is_absolute() else app_dir() / path

# Торговая секция [trading]: SQLite-backed candle simulation.
# При отсутствии секции (старые конфиги без торговых дефолтов) режим NotifyOnly.
INITIAL_DEPOSIT = int(_CONFIG.get("initial_deposit", 100_000))
MAX_RISK_PCT = float(_CONFIG.get("max_risk_pct", 2.0))
JOURNAL_FILE = _CONFIG.get("journal_file", "trade_journal.csv")
POSITIONS_FILE = _CONFIG.get("positions_file") or derived_positions_file(JOURNAL_FILE)
CLEARING_TIMES = list(_CONFIG.get("clearing_times", ["14:05", "19:00"]))
DATABASE_FILE = _CONFIG.get("database_file", "trades.sqlite3")
AUDIT_FILE = _CONFIG.get("audit_file", "trade_audit.log")
AUDIT_MAX_BYTES = _CONFIG.get("audit_max_bytes", 10_485_760)
AUDIT_BACKUP_COUNT = _CONFIG.get("audit_backup_count", 5)
RISK_LIMITS = dict(_CONFIG.get("risk_limits", {}))
TRADE_MANAGEMENT_PROFILES = dict(_CONFIG.get("trade_management_profiles", {}))


def trading_enabled() -> bool:
    """Признак активного торгового режима (наличие секции `[trading]` после слияния конфигов).

    В дефолтной сборке секция всегда есть → торговый режим активен. Отсутствие
    всех ключей (старый robot.toml без дефолтов) оставляет `NotifyOnlyExecutionPort`.
    """
    return any(
        key in _CONFIG
        for key in (
            "initial_deposit",
            "database_file",
        )
    )
