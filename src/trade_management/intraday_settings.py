"""Параметры сессионной защиты внутридневных сделок: секция ``[trading.intraday]``."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping

from src.scheduler.timing import tf_period_minutes

# Имена сессий конфигурации → типы окон расписания брокера (TradingSchedules).
SESSION_RAW_TYPES: Mapping[str, frozenset[str]] = {
    "morning": frozenset({"regular_trading_session_morning"}),
    "main": frozenset({"regular_trading_session_main"}),
    "evening": frozenset({"regular_trading_session_evening"}),
    "weekend": frozenset({"holiday_trading_session_morning", "holiday_trading_session_evening"}),
}
SESSION_NAMES = tuple(SESSION_RAW_TYPES)
_MINUTE_KEYS = ("no_entry_before_close_min", "flatten_before_close_min")
_BOOL_KEYS = ("enabled", "keep_if_tp1_and_breakeven")


@dataclass(frozen=True)
class IntradaySettings:
    """Правило: короткие сделки входят только в разрешённых сессиях и не ночуют без защиты."""

    enabled: bool = True
    max_timeframe: str = "15m"
    entry_sessions: tuple[str, ...] = ("morning", "main")
    no_entry_before_close_min: int = 60
    flatten_before_close_min: int = 20
    keep_if_tp1_and_breakeven: bool = True

    def __post_init__(self) -> None:
        for key in _BOOL_KEYS:
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f"{key} должен быть true или false")
        if not isinstance(self.max_timeframe, str):
            raise ValueError("max_timeframe должен быть строкой таймфрейма")
        try:
            tf_period_minutes(self.max_timeframe)
        except ValueError:
            raise ValueError(f"max_timeframe: неизвестный таймфрейм {self.max_timeframe!r}") from None
        sessions = self.entry_sessions
        if isinstance(sessions, list):
            sessions = tuple(sessions)
            object.__setattr__(self, "entry_sessions", sessions)
        if not isinstance(sessions, tuple) or any(not isinstance(name, str) for name in sessions):
            raise ValueError("entry_sessions должен быть списком имён сессий")
        unknown = [name for name in sessions if name not in SESSION_RAW_TYPES]
        if unknown:
            raise ValueError(f"entry_sessions: неизвестные сессии {unknown}; допустимые: {list(SESSION_NAMES)}")
        if len(set(sessions)) != len(sessions):
            raise ValueError("entry_sessions: сессии не должны повторяться")
        for key in _MINUTE_KEYS:
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} должен быть целым числом >= 0, получено {value!r}")
        if self.flatten_before_close_min > self.no_entry_before_close_min:
            raise ValueError("flatten_before_close_min не может быть больше no_entry_before_close_min")

    @property
    def max_minutes(self) -> int:
        return tf_period_minutes(self.max_timeframe)

    @property
    def raw_types(self) -> frozenset[str]:
        """Типы окон расписания, в которых разрешён внутридневной вход."""
        return frozenset().union(*(SESSION_RAW_TYPES[name] for name in self.entry_sessions))

    def is_intraday(self, timeframe: str | None) -> bool:
        """Сделка внутридневная по длительности таймфрейма плана; неизвестный — свинговая."""
        if not timeframe:
            return False
        try:
            return tf_period_minutes(timeframe) <= self.max_minutes
        except ValueError:
            return False

    def as_config(self) -> dict[str, Any]:
        return {f.name: list(getattr(self, f.name)) if f.name == "entry_sessions" else getattr(self, f.name)
                for f in fields(self)}

    @classmethod
    def from_config(cls, section: Mapping[str, Any]) -> IntradaySettings:
        """Собирает параметры из таблицы; незнакомый ключ → ValueError с его именем."""
        allowed = {f.name for f in fields(cls)}
        unknown = sorted(set(section) - allowed)
        if unknown:
            raise ValueError(f"незнакомые ключи {unknown}")
        values = dict(section)
        if "entry_sessions" in values:
            if not isinstance(values["entry_sessions"], list):
                raise ValueError("entry_sessions должен быть списком имён сессий")
            values["entry_sessions"] = tuple(values["entry_sessions"])
        return cls(**values)
