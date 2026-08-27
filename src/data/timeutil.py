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
