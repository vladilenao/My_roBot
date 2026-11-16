"""Описание отрисовки индикатора для графика UI (add-indicators-api).

Робот отдаёт роли линий, панель и уровни, UI раскладывает панели по
описанию и сам выбирает цвета. Колонка линии — колонка результата
``compute()`` того же экземпляра индикатора.
"""

from __future__ import annotations

from dataclasses import dataclass

PANE_PRICE = "price"
PANE_SEPARATE = "separate"
KIND_LINE = "line"
KIND_HISTOGRAM = "histogram"


@dataclass(frozen=True)
class ChartLine:
    """Линия индикатора: роль, колонка результата ``compute()`` и вид."""

    name: str
    column: str
    kind: str = KIND_LINE


@dataclass(frozen=True)
class IndicatorChart:
    """Как рисовать индикатор: ключ вида, подпись, параметры, панель, уровни и линии."""

    key: str
    title: str
    params: dict[str, float]
    pane: str
    lines: tuple[ChartLine, ...]
    levels: tuple[float, ...] = ()
    # Заливка между двумя линиями по их ролям — у облака MA.
    fill: tuple[str, str] | None = None
