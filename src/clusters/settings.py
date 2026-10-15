"""Параметры кластерного фильтра: ключ привязки ``clusters`` и секция ``[clusters]``."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any, Mapping

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)
ENFORCE_LEVELS = ("veto", "veto_strong")
_CONSUMER_GROUP = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NUMBER_KEYS = ("pre_move_veto_sigma", "initiative_max_sigma", "extreme_position")
_INTEGER_KEYS = ("climax_window_min", "initiative_window_min", "sigma_days", "max_consumer_lag_sec")


@dataclass(frozen=True)
class ClusterSettings:
    """Пороги алгоритма ``clusters-v1`` и адрес потока; Redis — из ``[market_structure]``."""

    stream: str = "cluster.events"
    consumer_group: str = "robot-v1"
    pre_move_veto_sigma: float = 1.5
    initiative_max_sigma: float = 0.5
    extreme_position: float = 0.8
    climax_window_min: int = 30
    initiative_window_min: int = 60
    sigma_days: int = 10
    max_consumer_lag_sec: int = 60
    enforce_level: str = "veto"

    def __post_init__(self) -> None:
        if not isinstance(self.stream, str) or not self.stream.strip():
            raise ValueError("stream должен быть непустой строкой")
        if not isinstance(self.consumer_group, str) or not _CONSUMER_GROUP.fullmatch(self.consumer_group):
            raise ValueError("consumer_group — строчные латинские буквы, цифры и дефис, до 63 символов")
        for key in _NUMBER_KEYS:
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{key} должен быть числом > 0, получено {value!r}")
        for key in _INTEGER_KEYS:
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{key} должен быть целым числом >= 1, получено {value!r}")
        if self.initiative_max_sigma >= self.pre_move_veto_sigma:
            raise ValueError("initiative_max_sigma должен быть меньше pre_move_veto_sigma")
        if self.extreme_position > 1:
            raise ValueError("extreme_position должен быть в (0, 1]")
        if self.climax_window_min > self.initiative_window_min:
            raise ValueError("climax_window_min не может быть больше initiative_window_min")
        if self.enforce_level not in ENFORCE_LEVELS:
            raise ValueError(f"enforce_level должен быть одним из {list(ENFORCE_LEVELS)}, получено {self.enforce_level!r}")

    @classmethod
    def from_config(cls, section: Mapping[str, Any]) -> ClusterSettings:
        """Собирает параметры из таблицы ``[clusters]``; незнакомый ключ → ValueError без значения."""
        allowed = {f.name for f in fields(cls)}
        unknown = set(section) - allowed
        if unknown:
            raise ValueError(f"незнакомые ключи {sorted(unknown)}; допустимые: {sorted(allowed)}")
        return cls(**dict(section))


def check_mode(value: object) -> str:
    """Режим привязки ``clusters``: off / shadow / enforce."""
    if value not in MODES:
        raise ValueError(f"clusters должен быть одним из {list(MODES)}, получено {value!r}")
    return str(value)
