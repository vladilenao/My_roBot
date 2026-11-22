from __future__ import annotations

import pandas as pd


def to_naive(dt) -> pd.Timestamp:
    """Приводит время/наследника datetime к tz-naive pandas.Timestamp (UTC без пояса).

    Если значение несёт часовой пояс — срезает его; если уже naive — возвращает как есть.
    Является единой точкой нормализации времени в роботе (см. change normalize-timezone-naive).
    """
    ts = pd.Timestamp(dt)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


def to_aware_utc(dt) -> pd.Timestamp:
    """Приводит время к tz-aware UTC (исходящая граница API).

    Naive значение трактуется как UTC по договорённости и локализуется;
    aware значение конвертируется в UTC. Используется только на исходящем
    крае запросов к Tinkoff API, который требует aware-время.
    """
    ts = pd.Timestamp(dt)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def to_aware_utc_series(values: pd.Series) -> pd.Series:
    """``values.map(to_aware_utc)`` одной векторной операцией.

    Столбец времени минутной истории контракта переводится в aware UTC на
    каждом тике; поэлементный ``map`` стоил десятки миллисекунд на тысячах
    свечей и дорожал с каждой минутой прогона. Столбец без единого
    datetime-типа (смесь значений) переводится поэлементно, как прежде.
    """
    if pd.api.types.is_datetime64_any_dtype(values.dtype):
        if values.dt.tz is None:
            return values.dt.tz_localize("UTC")
        return values.dt.tz_convert("UTC")
    return values.map(to_aware_utc)


def to_utc_naive(dt) -> pd.Timestamp:
    """Входной факт сначала конвертируется в UTC, потом лишается зоны.

    Отличается от legacy to_naive, который сохраняется для внутренних уже
    нормализованных UTC значений и исторических интерфейсов.
    """
    return to_aware_utc(dt).tz_localize(None)
