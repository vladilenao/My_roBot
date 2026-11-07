"""Индикаторы стратегии привязки на свечах рыночной базы ядра (add-indicators-api): только чтение.

``GET /api/v1/candles/{instrument}/{timeframe}/indicators?binding=<id>`` —
значения индикаторов стратегии привязки на том же окне, что у ресурса
свечей. Имя инструмента, таймфрейм и окно разбирает ``candles_view``.

Режим ``strategy`` (по умолчанию) повторяет живой анализ: точка свечи —
последняя строка расчёта на хвосте до глубины расчёта привязки
(``binding_depths`` с ``[robot] analysis_depth``), до минимума допуска
(``binding_requirements``) — ``null``. Режим ``continuous`` — один расчёт
на окне с прогревом, как в терминалах.

Расчёт идёт в процессе ядра рядом с полосами данных, поэтому одновременно
считается не больше одного запроса, а точки завершённых свечей режима
``strategy`` кэшируются.
"""

from __future__ import annotations

import math
import re
import threading
from collections import OrderedDict
from http import HTTPStatus
from urllib.parse import quote, unquote

import pandas as pd

from src.bot import control_views as views
from src.bot.candles_view import (CandlesError, _one, _text, earlier_query, resolve_series, series_href,
                                  window_bounds, window_query)
from src.broker_contour.bindings import binding_catalog, instrument_bindings
from src.data.market_store import SOURCE_TBANK
from src.history.warmup import binding_depths, binding_requirements
from src.logging_setup import get_logger
from src.strategies.registry import get_strategy

log = get_logger(__name__)

MODE_STRATEGY = "strategy"
MODE_CONTINUOUS = "continuous"
MODES = (MODE_STRATEGY, MODE_CONTINUOUS)
LIMIT_MAX = 1000
# Прогрев режима continuous: след начальной точки EMA-26 и RMA-14 после 300 свечей — порядка 10⁻¹⁰.
CONTINUOUS_WARMUP = 300
CACHE_POINTS = 100_000
_PATH = re.compile(r"^/api/v1/candles/([^/]+)/([^/]+)/indicators$")
_FRAME_COLUMNS = ("open", "high", "low", "close")


def is_indicators_path(path: str) -> bool:
    return _PATH.match(path) is not None


def indicators_href(name: str, timeframe: str) -> str:
    return series_href(name, timeframe) + "/indicators"


class IndicatorsView:
    """Ресурс индикаторов: привязки и стратегии конфигурации, кэш точек и очередь расчёта."""

    def __init__(self, *, share_strategies: dict, future_strategies: dict, strategy_map: dict, profiles: dict,
                 triple_screen, ladder: tuple[str, ...], depth: int, cache_points: int = CACHE_POINTS) -> None:
        self._assignments = {item.id.lower(): item for strategies in (share_strategies, future_strategies)
                             for items in strategies.values() for item in items}
        self._catalog = binding_catalog(share_strategies, future_strategies)
        self._strategy_map = strategy_map
        self._profiles, self._triple_screen, self._ladder, self._depth = profiles, triple_screen, ladder, depth
        self._binding_depths: dict[str, tuple[int, int]] = {}
        self._cache: OrderedDict[tuple, tuple] = OrderedDict()
        self._cache_points = cache_points
        # Один расчёт за раз: два открытых графика не удваивают спор за GIL с полосами данных ядра.
        self._lock = threading.Lock()
        # Сколько точек посчитано заново — для проверки кэша.
        self.computed_points = 0

    def known(self, binding_id: str) -> bool:
        """Привязка есть в текущей конфигурации — у неё может быть ``_links.indicators``."""
        return binding_id.lower() in self._assignments

    def page(self, store, path: str, query: dict[str, list[str]], now=None) -> dict:
        match = _PATH.match(path)
        if match is None:
            raise CandlesError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
        raw, timeframe = (unquote(part) for part in match.groups())
        lower, upper, limit = window_bounds(timeframe, query, now, LIMIT_MAX)
        wanted = _one(query, "binding")
        if not wanted:
            raise CandlesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "binding — id привязки из [strategies.*].")
        mode = _one(query, "mode") or MODE_STRATEGY
        if mode not in MODES:
            raise CandlesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", f"mode — один из: {', '.join(MODES)}.")
        row = resolve_series(store, raw, timeframe)
        binding = self._assignments.get(wanted.lower())
        if binding is None:
            raise CandlesError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Привязки {wanted} нет в конфигурации.",
                               self._suggestions(row))
        config = self._strategy_map[binding.strategy]
        minimum, depth = self._depths(binding, config)
        history = depth - 1 if mode == MODE_STRATEGY else CONTINUOUS_WARMUP
        candles, earlier, prior = store.candle_window_with_history(
            source=SOURCE_TBANK, instrument_uid=row["instrument_uid"], interval=row["interval"],
            lower=lower, upper=upper, limit=limit, history=history)
        name = row["name"]
        try:
            with self._lock:
                if mode == MODE_STRATEGY:
                    key = (binding.id, repr(config.indicators), minimum, depth, row["instrument_uid"], timeframe)
                    points = self._strategy_points(config.indicators, prior, candles, minimum, depth, key)
                else:
                    points = _continuous_points(config.indicators, prior, candles)
        except Exception as exc:
            # Без данных кадра и текста исключения: в журнал — тип, привязка, короткое имя и таймфрейм.
            log.error("Индикаторы %s %s %s не посчитаны: %s", binding.id, name, timeframe, type(exc).__name__)
            raise CandlesError(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL",
                               "Индикаторы не посчитаны: ошибка расчёта.") from None
        base = indicators_href(name, timeframe)
        prefix = (f"binding={quote(binding.id, safe='')}", f"mode={mode}")
        found = {"self": base + window_query(query, lower, upper, limit, prefix),
                 "candles": series_href(name, timeframe) + window_query(query, lower, upper, limit)}
        if earlier:
            found["earlier"] = f"{base}?{'&'.join(prefix)}&{earlier_query(candles, lower, upper, limit)}"
        return {"instrument": name, "instrument_type": row["instrument_type"], "timeframe": timeframe,
                "binding": {"id": binding.id, "strategy": str(binding.strategy), "timeframe": binding.timeframe},
                "binding_timeframe": timeframe == binding.timeframe,
                "mode": mode, "depth": depth if mode == MODE_STRATEGY else None,
                "from": None if lower is None else _text(lower), "to": _text(upper),
                "times": [candle["open_time"] for candle in candles],
                "last_complete": candles[-1]["complete"] if candles else True,
                "indicators": [_series(indicator.chart, points) for indicator in config.indicators],
                "_links": views.links(**found)}

    def _depths(self, binding, config) -> tuple[int, int]:
        """Минимум допуска и глубина расчёта привязки на её таймфрейме — как у живого анализа."""
        if binding.id not in self._binding_depths:
            strategy = get_strategy(binding.strategy, config)
            arguments = (binding, strategy, self._profiles, self._triple_screen, self._ladder)
            self._binding_depths[binding.id] = (binding_requirements(*arguments)[binding.timeframe],
                                                binding_depths(*arguments, depth=self._depth)[binding.timeframe])
        return self._binding_depths[binding.id]

    def _suggestions(self, row: dict) -> list[str]:
        card = {"ticker": row["ticker"], "short_name": row["short_name"], "name": row["name"],
                "instrument_type": row["instrument_type"]}
        return [item["id"] for item in instrument_bindings(card, self._catalog)]

    def _strategy_points(self, indicators, prior, candles, minimum, depth, key) -> list[dict | None]:
        """Точка свечи — последняя строка расчёта на хвосте до ``depth`` свечей, ею заканчивающемся."""
        if not indicators:
            return [None] * len(candles)
        frame = _frame(prior + candles)
        points: list[dict | None] = []
        for offset, candle in enumerate(candles):
            position = len(prior) + offset
            if position + 1 < minimum:
                points.append(None)
                continue
            # Кэш — только завершённые свечи на полном хвосте: короткий хвост начала ряда
            # изменится, если в базу догрузят свечи раньше.
            stable = candle["complete"] and position + 1 >= depth
            cache_key = key + (candle["open_time"],)
            if stable and cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                points.append(dict(self._cache[cache_key]))
                continue
            tail = frame.iloc[max(0, position - depth + 1):position + 1].reset_index(drop=True)
            point = _last_row(indicators, tail)
            self.computed_points += 1
            if stable:
                self._cache[cache_key] = tuple(point.items())
                if len(self._cache) > self._cache_points:
                    self._cache.popitem(last=False)
            points.append(point)
        return points


def _continuous_points(indicators, prior, candles) -> list[dict | None]:
    if not indicators or not candles:
        return [None] * len(candles)
    data = _compute(indicators, _frame(prior + candles)).iloc[len(prior):]
    columns = _columns(indicators)
    return [{column: _number(value) for column, value in zip(columns, values)}
            for values in data[list(columns)].itertuples(index=False, name=None)]


def _frame(candles: list[dict]) -> pd.DataFrame:
    """Кадр той же формы, что у живого анализа: время открытия и OHLCV числами."""
    frame = pd.DataFrame({"datetime": pd.to_datetime([candle["open_time"] for candle in candles], utc=True)})
    for column in _FRAME_COLUMNS:
        frame[column] = [float(candle[column]) for candle in candles]
    frame["volume"] = [float(candle["volume"]) for candle in candles]
    return frame


def _compute(indicators, frame: pd.DataFrame) -> pd.DataFrame:
    data = frame
    for indicator in indicators:
        data = indicator.compute(data)
    return data


def _columns(indicators) -> tuple[str, ...]:
    return tuple(line.column for indicator in indicators for line in indicator.chart.lines)


def _last_row(indicators, tail: pd.DataFrame) -> dict:
    row = _compute(indicators, tail).iloc[-1]
    return {column: _number(row[column]) for column in _columns(indicators)}


def _series(chart, points: list[dict | None]) -> dict:
    item = {"key": chart.key, "title": chart.title, "params": dict(chart.params), "pane": chart.pane,
            "levels": list(chart.levels)}
    if chart.fill is not None:
        item["fill"] = {"between": list(chart.fill)}
    item["lines"] = [{"name": line.name, "kind": line.kind,
                      "values": [None if point is None else point[line.column] for point in points]}
                     for line in chart.lines]
    return item


def _number(value) -> float | None:
    """8 значащих цифр; ``NaN`` и бесконечность — ``null``."""
    if value is None:
        return None
    number = float(value)
    return float(f"{number:.8g}") if math.isfinite(number) else None

