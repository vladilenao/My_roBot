"""Параметры live-ingestion секции ``[market_data]``.

Числа по умолчанию — предварительный инженерный профиль design 11c, а не
измеренная гарантия брокера. Ограничения API (300 подписок, 100 control-запросов
и 600 unary RPC в минуту, ping 5000–180000 мс) задают верхние пределы.
Ни один параметр не наследуется от ``[tick] timeout_secs``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from math import isfinite
from typing import Any, Mapping

LIVE_TRANSPORTS = ("polling", "stream")
NATIVE_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")

# имя ключа → (вид, минимум, максимум, включительный минимум)
_LIMITS: dict[str, tuple[str, float | None, float | None, bool]] = {
    "candle_publication_timeout_secs": ("number", 0, None, False),
    "stream_ack_timeout_seconds": ("number", 0, None, False),
    "stream_ping_interval_ms": ("int", 5000, 180000, True),
    "stream_health_timeout_seconds": ("number", 0, None, False),
    "stream_buffer_max_events": ("int", 1, None, True),
    "stream_buffer_max_bytes": ("int", 1, None, True),
    "stream_control_queue_max_messages": ("int", 1, None, True),
    "stream_subscription_soft_limit": ("int", 1, 300, True),
    "stream_control_requests_per_minute": ("int", 1, 100, True),
    "stream_reconnect_initial_secs": ("number", 0, None, False),
    "stream_reconnect_max_secs": ("number", 0, None, False),
    "stream_reconnect_jitter_pct": ("number", 0, 100, True),
    "stream_control_cooldown_secs": ("number", 0, None, True),
    "unary_requests_per_minute": ("int", 1, 600, True),
    "recovery_round_timeout_secs": ("number", 0, None, False),
    "recovery_max_attempts": ("int", 1, None, True),
    "recovery_backoff_max_secs": ("number", 0, None, False),
    "recovery_recheck_interval_secs": ("number", 0, None, False),
    "recent_reconciliation_interval_secs": ("number", 0, None, False),
    "recent_reconciliation_bars": ("int", 1, None, True),
    "subscription_check_interval_secs": ("number", 0, None, False),
    "status_repeat_interval_secs": ("number", 0, None, False),
    "schedule_refresh_interval_secs": ("number", 0, None, False),
    "schedule_max_age_secs": ("number", 0, None, False),
    "schedule_horizon_days": ("int", 0, None, True),
    "instrument_requests_per_minute": ("int", 1, 200, True),
    "evidence_window_secs": ("number", 0, 3600, False),
    "completeness_rescan_interval_secs": ("number", 0, None, False),
    "lane_status_log_interval_secs": ("number", 0, None, False),
}

# Полосы и сервисы источника (add-independent-market-data-lanes, design 2).
QUOTE_LANES = ("live", "evidence", "completeness")
INSTRUMENT_LANES = ("interactive", "calendar")
_SHARE_KEYS = {"quote_lane_shares": QUOTE_LANES, "instrument_lane_shares": INSTRUMENT_LANES}
_SCHEDULE_KEYS = ("evidence_retry_secs", "completeness_recheck_secs")
# Параметры прежнего раунда восстановления: при независимых полосах не
# используются, но старые конфиги с ними принимаются.
DEPRECATED_WITH_LANES = ("recovery_round_timeout_secs", "recovery_max_attempts",
                         "recovery_backoff_max_secs", "recovery_recheck_interval_secs")


@dataclass(frozen=True)
class MarketDataSettings:
    live_transport: str = "polling"
    stream_fallback_polling: bool = True
    candle_publication_timeout_secs: float = 65
    stream_ack_timeout_seconds: float = 30
    stream_ping_interval_ms: int = 15000
    stream_health_timeout_seconds: float = 45
    stream_buffer_max_events: int = 50000
    stream_buffer_max_bytes: int = 64 * 1024 * 1024
    stream_control_queue_max_messages: int = 128
    stream_subscription_soft_limit: int = 280
    stream_control_requests_per_minute: int = 90
    stream_reconnect_initial_secs: float = 1
    stream_reconnect_max_secs: float = 30
    stream_reconnect_jitter_pct: float = 20
    stream_control_cooldown_secs: float = 60
    unary_requests_per_minute: int = 300
    recovery_round_timeout_secs: float = 900
    recovery_max_attempts: int = 5
    recovery_backoff_max_secs: float = 300
    recovery_recheck_interval_secs: float = 300
    recent_reconciliation_interval_secs: float = 300
    recent_reconciliation_bars: int = 2
    subscription_check_interval_secs: float = 300
    status_repeat_interval_secs: float = 60
    schedule_refresh_interval_secs: float = 3600
    schedule_max_age_secs: float = 129600
    schedule_horizon_days: int = 7
    calendar_evidence: bool = True
    candle_publication_timeout_by_tf: dict[str, float] = field(default_factory=dict)
    # Независимые полосы получения данных. Числа — инженерные гипотезы:
    # половина квоты брокера остаётся другим приложениям пользователя.
    independent_lanes: bool = False  # включается по умолчанию после приёмки (design, план перехода)
    instrument_requests_per_minute: int = 150
    quote_lane_shares: dict[str, float] = field(
        default_factory=lambda: {"live": 50, "evidence": 25, "completeness": 25})
    instrument_lane_shares: dict[str, float] = field(
        default_factory=lambda: {"interactive": 50, "calendar": 50})
    evidence_window_secs: float = 3300
    evidence_retry_secs: tuple[float, ...] = (60, 180, 600)
    completeness_recheck_secs: tuple[float, ...] = (3600, 86400)
    completeness_rescan_interval_secs: float = 3600
    lane_status_log_interval_secs: float = 300

    def __post_init__(self):
        for name in setting_names():
            validate_setting(name, getattr(self, name))
        for name in _SCHEDULE_KEYS:  # TOML отдаёт список, профиль хранит кортеж
            object.__setattr__(self, name, tuple(getattr(self, name)))
        validate_combination(asdict(self))

    def lane_shares(self) -> dict[str, dict[str, float]]:
        """Доли полос для арбитра: сервис → полоса → процент."""
        return {"quotes": dict(self.quote_lane_shares), "instruments": dict(self.instrument_lane_shares)}

    def deprecated_overrides(self) -> tuple[str, ...]:
        """Заданные в конфиге или изменённые параметры прежнего раунда, которые полосы не используют."""
        if not self.independent_lanes:
            return ()
        defaults = MarketDataSettings.__dataclass_fields__
        given = getattr(self, "_given", frozenset())
        return tuple(name for name in DEPRECATED_WITH_LANES
                     if name in given or getattr(self, name) != defaults[name].default)

    @classmethod
    def from_flat(cls, flat: Mapping[str, Any]) -> "MarketDataSettings":
        """Эффективные значения из плоских ключей ``market_data_<имя>``."""
        values = {name: flat[f"market_data_{name}"] for name in setting_names() if f"market_data_{name}" in flat}
        settings = cls(**values)
        # Ключи, заданные в файлах конфигурации (а не профилем по умолчанию), —
        # для предупреждения об устаревших параметрах.
        given = flat.get("market_data_explicit_keys") or ()
        object.__setattr__(settings, "_given", frozenset(key.removeprefix("market_data_") for key in given))
        return settings

    def as_dict(self) -> dict[str, Any]:
        """Эффективный профиль для диагностики; секретов здесь нет."""
        return asdict(self)


def setting_names() -> tuple[str, ...]:
    return tuple(item.name for item in fields(MarketDataSettings))


class SettingError(ValueError):
    pass


def validate_setting(name: str, value: Any) -> Any:
    if name == "live_transport":
        if value not in LIVE_TRANSPORTS:
            raise SettingError(f"live_transport должен быть одним из {', '.join(LIVE_TRANSPORTS)}, получено {value!r}")
        return value
    if name == "stream_fallback_polling":
        if not isinstance(value, bool):
            raise SettingError("stream_fallback_polling должен быть логическим значением")
        return value
    if name in ("calendar_evidence", "independent_lanes"):
        if not isinstance(value, bool):
            raise SettingError(f"{name} должен быть логическим значением")
        return value
    if name in _SHARE_KEYS:
        lanes = _SHARE_KEYS[name]
        if not isinstance(value, Mapping) or set(value) != set(lanes):
            raise SettingError(f"{name} должен задавать доли полос {', '.join(lanes)}")
        for lane, pct in value.items():
            if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not isfinite(pct) or not 0 <= pct <= 100:
                raise SettingError(f"{name}[{lane!r}] должен быть процентом 0..100")
        if abs(sum(value.values()) - 100) > 1e-9:
            raise SettingError(f"Сумма {name} должна быть 100")
        return value
    if name in _SCHEDULE_KEYS:
        if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
            raise SettingError(f"{name} должен быть списком секунд")
        items = list(value)
        if not items or any(isinstance(item, bool) or not isinstance(item, (int, float)) or not isfinite(item) or item <= 0
                            for item in items):
            raise SettingError(f"{name} должен содержать хотя бы одно конечное число секунд > 0")
        if any(later <= earlier for earlier, later in zip(items, items[1:])):
            raise SettingError(f"{name} должен строго возрастать")
        return tuple(items)
    if name == "candle_publication_timeout_by_tf":
        if not isinstance(value, dict):
            raise SettingError("candle_publication_timeout_by_tf должен быть отображением")
        for timeframe, timeout in value.items():
            if timeframe not in NATIVE_TIMEFRAMES:
                raise SettingError(f"candle_publication_timeout_by_tf: неизвестный ТФ {timeframe!r}")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not isfinite(timeout) or timeout <= 0:
                raise SettingError(f"candle_publication_timeout_by_tf[{timeframe!r}] должен быть конечным числом > 0")
        return value
    kind, lower, upper, inclusive = _LIMITS[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise SettingError(f"{name} должен быть конечным числом")
    if kind == "int" and not isinstance(value, int):
        raise SettingError(f"{name} должен быть целым числом")
    if lower is not None and (value < lower or (not inclusive and value == lower)):
        raise SettingError(f"{name} должен быть {'>=' if inclusive else '>'} {lower}")
    if upper is not None and value > upper:
        raise SettingError(f"{name} не должен превышать {upper}")
    return value


def validate_combination(values: Mapping[str, Any]) -> None:
    initial = values.get("stream_reconnect_initial_secs")
    maximum = values.get("stream_reconnect_max_secs")
    if initial is not None and maximum is not None and initial > maximum:
        raise SettingError("stream_reconnect_initial_secs не должен превышать stream_reconnect_max_secs")
    ping = values.get("stream_ping_interval_ms")
    health = values.get("stream_health_timeout_seconds")
    if ping is not None and health is not None and health * 1000 <= ping:
        raise SettingError("stream_health_timeout_seconds должен превышать интервал ping")


def candle_publication_timeout(settings: MarketDataSettings, timeframe: str) -> float:
    """Эффективный срок публикации конкретного нативного таймфрейма."""
    if timeframe not in NATIVE_TIMEFRAMES:
        raise ValueError(f"Неподдерживаемый ТФ {timeframe!r}")
    return settings.candle_publication_timeout_by_tf.get(timeframe, settings.candle_publication_timeout_secs)
