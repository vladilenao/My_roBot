"""Порт рыночных служб и потребности торгового контура.

Контур получает данные только через ``MarketServices``: в эмуляции порт
реализует ядро, в историческом прогоне — историческая служба данных. Свои
потребности контур передаёт объектом ``ContourDemand`` и сам читает для этого
свою финансовую базу; службы её не открывают (refactor-core-contour-split).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Protocol

from src.config import ACTIVE_TIMEFRAMES
from src.instruments.model import Instrument


def _timeframes() -> list[str]:
    """Активные ТФ привязок плюс базовый 1m защиты — общий ритм служб и контура."""
    return sorted(set(ACTIVE_TIMEFRAMES) | {"1m"})


@dataclass(frozen=True)
class ContourDemand:
    """Что контуру нужно от рыночных служб.

    ``obligations`` — обязательства незавершённых сделок для владельца входа;
    ``None`` — у контура нет финансовой базы (NotifyOnly), обязательств нет.
    ``pattern_pairs`` — пары (инструмент, ТФ привязки), чьи закрытые бары ядро
    ставит воркеру паттернов (add-contour-market-feed).
    """

    instruments: tuple[Instrument, ...]
    aliases: Mapping[str, str] = field(default_factory=dict)
    obligations: object = None
    history_requests: tuple[tuple[Instrument, str, int], ...] = ()
    execution_lower: tuple[tuple[Instrument, datetime], ...] = ()
    pattern_pairs: tuple[tuple[Instrument, str], ...] = ()


class MarketServices(Protocol):
    """Рыночные службы, которыми пользуется торговый контур."""

    timeline: object
    data_cache: object
    market_store: object
    market_calendar: object
    request_budget: object

    def cluster_gate(self, state_dir): ...

    def serve(self, demand: ContourDemand) -> Callable[[], bool] | None: ...

    def update_obligations(self, obligations) -> None: ...

    def acknowledge_tick(self) -> None: ...

    def close(self) -> None: ...
