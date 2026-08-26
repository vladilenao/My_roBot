"""Единственный ручной перечень имён стратегий для статических подсказок IDE.

Импортирует только typing: никакой связи с реестром и пакетами стратегий,
чтобы src.config не подтягивал тяжёлые зависимости.
"""

from typing import Literal

StrategyName = Literal["macd_rsi_stoch", "flat_triangle"]
