"""Параметры live-ingestion секции ``[market_data]``.

Числа по умолчанию — предварительный инженерный профиль design 11c, а не
измеренная гарантия брокера. Ограничения API (300 подписок, 100 control-запросов
и 600 unary RPC в минуту, ping 5000–180000 мс) задают верхние пределы.
Ни один параметр не наследуется от ``[tick] timeout_secs``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from math import isfinite
from typing import Any, Mapping

LIVE_TRANSPORTS = ("polling", "stream")

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
}


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
    unary_requests_per_minute: int = 120
    recovery_round_timeout_secs: float = 900
    recovery_max_attempts: int = 5
    recovery_backoff_max_secs: float = 300
    recovery_recheck_interval_secs: float = 300
    recent_reconciliation_interval_secs: float = 300
    recent_reconciliation_bars: int = 2
    subscription_check_interval_secs: float = 300
    status_repeat_interval_secs: float = 60

    def __post_init__(self):
        for name in setting_names():
            validate_setting(name, getattr(self, name))
        validate_combination(asdict(self))

    @classmethod
    def from_flat(cls, flat: Mapping[str, Any]) -> "MarketDataSettings":
        """Эффективные значения из плоских ключей ``market_data_<имя>``."""
        values = {name: flat[f"market_data_{name}"] for name in setting_names() if f"market_data_{name}" in flat}
        return cls(**values)

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
