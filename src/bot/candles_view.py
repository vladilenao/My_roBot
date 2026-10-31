"""Свечи рыночной базы ядра для графиков UI (add-candles-api): только чтение.

Имена инструментов — те же, что в командах контуров: короткое имя контракта
(``NG-12.26``) или тикер акции (``SBER``). Биржевой тикер и ``instrument_uid``
наружу не выходят: на них и на код семейства — ``404`` с подсказкой коротких
имён в ``suggestions``. Таймфреймы — только родные таймфреймы брокера, которые
лежат в базе: один таймфрейм из другого не собирается.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import quote, unquote

import pandas as pd

from src.bot import control_views as views
from src.data.ingestion_settings import NATIVE_TIMEFRAMES
from src.data.market_store import SOURCE_TBANK
from src.instruments.futures import family_for
from src.instruments.model import SHARE_INSTRUMENT_TYPE
from src.scheduler.timing import tf_period_minutes

LIMIT_DEFAULT = 500
LIMIT_MAX = 5000
_SERIES = re.compile(r"^/api/v1/candles/([^/]+)/([^/]+)$")
# Месяц и год экспирации в коротком имени контракта: NG-12.26.
_EXPIRY = re.compile(r"-(\d{2})\.(\d{2})$")


class CandlesError(Exception):
    """Отказ ресурса свечей: код HTTP, код ошибки, причина и подсказки."""

    def __init__(self, status: HTTPStatus, code: str, detail: str, suggestions: list[str] | None = None):
        super().__init__(detail)
        self.status, self.code, self.detail, self.suggestions = status, code, detail, suggestions


def series_href(name: str, timeframe: str) -> str:
    return f"{views.CANDLES}/{quote(name, safe='')}/{timeframe}"


def instrument_series_href(name: str) -> str:
    return f"{views.CANDLES}?instrument={quote(name, safe='')}"


def series_list(store, query: dict[str, list[str]]) -> dict:
    """``GET /api/v1/candles``: ряды по инструменту, затем по длительности таймфрейма."""
    raw = _one(query, "instrument")
    if "instrument" in query and not raw:
        raise CandlesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "instrument — одно непустое имя инструмента.")
    rows = _named(store)
    if raw:
        rows = [row for row in rows if row["name"].upper() == raw.upper()]
    # Ссылка повторяет фильтр только каноническим именем: запрошенный биржевой тикер наружу не выходит.
    self_href = instrument_series_href(rows[0]["name"]) if raw and rows else views.CANDLES
    return {"items": [_series_item(row) for row in sorted(rows, key=_order)],
            "_links": views.links(self=self_href, home=views.HOME)}


def series_window(store, path: str, query: dict[str, list[str]], now: datetime | None = None) -> dict:
    """``GET /api/v1/candles/{instrument}/{timeframe}``: последние ``limit`` свечей окна ``[from, to)``."""
    match = _SERIES.match(path)
    if match is None:
        raise CandlesError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
    raw, timeframe = (unquote(part) for part in match.groups())
    if timeframe not in NATIVE_TIMEFRAMES:
        raise CandlesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST",
                           f"Таймфрейм — один из: {', '.join(NATIVE_TIMEFRAMES)}.")
    lower = _moment(query, "from")
    upper = _moment(query, "to") or _ceil_second(now or datetime.now(timezone.utc))
    limit = _limit(query)
    if lower is not None and lower >= upper:
        raise CandlesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Начало окна from должно быть раньше конца to.")
    rows = _named(store)
    row = _resolve(rows, raw, timeframe)
    candles, earlier = store.candle_window(source=SOURCE_TBANK, instrument_uid=row["instrument_uid"],
                                           interval=row["interval"], lower=lower, upper=upper, limit=limit)
    name, to_text = row["name"], _text(upper)
    params = ([f"from={_text(lower)}"] if lower is not None else []) + \
             ([f"to={to_text}"] if "to" in query else []) + ([f"limit={limit}"] if "limit" in query else [])
    found = {"self": series_href(name, timeframe) + ("?" + "&".join(params) if params else ""),
             "series": instrument_series_href(name)}
    if earlier:
        bound = candles[0]["open_time"] if candles else _text(lower if lower is not None else upper)
        found["earlier"] = f"{series_href(name, timeframe)}?to={bound}&limit={limit}"
    return {"instrument": name, "instrument_type": row["instrument_type"], "timeframe": timeframe,
            "from": None if lower is None else _text(lower), "to": to_text, "candles": candles,
            "_links": views.links(**found)}


# --- имена ---------------------------------------------------------------------------

def _named(store) -> list[dict]:
    """Пары состава с каноническим именем; контракт без короткого имени наружу не выходит."""
    rows = []
    for row in store.candle_series(source=SOURCE_TBANK):
        name = row["short_name"] or (row["ticker"] if row["instrument_type"] == SHARE_INSTRUMENT_TYPE else None)
        if name:
            rows.append({**row, "name": name})
    return rows


def _resolve(rows: list[dict], raw: str, timeframe: str) -> dict:
    """Ряд по имени и таймфрейму; иначе ``404`` с подходящими именами или таймфреймами."""
    wanted = raw.upper()
    mine = [row for row in rows if row["name"].upper() == wanted]
    if mine:
        for row in mine:
            if row["interval"] == timeframe:
                return row
        frames = sorted({row["interval"] for row in mine}, key=tf_period_minutes)
        raise CandlesError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"У {mine[0]['name']} нет ряда {timeframe}.", frames)
    by_ticker = sorted({row["name"] for row in rows if row["ticker"].upper() == wanted})
    if by_ticker:
        raise CandlesError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Укажите короткое имя контракта.", by_ticker)
    family = [row for row in rows if row["instrument_type"] != SHARE_INSTRUMENT_TYPE
              and family_for(row["ticker"], row["short_name"] or "") == wanted]
    if family:
        raise CandlesError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Код семейства не принимается — укажите контракт.",
                           _contracts_first_active(family))
    raise CandlesError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Инструмента {raw} нет в рыночной базе.", [])


def _contracts_first_active(rows: list[dict]) -> list[str]:
    """Сначала контракты текущего состава по возрастанию экспирации, затем прошлые — новые первыми."""
    names: dict[str, bool] = {}
    for row in rows:
        names[row["name"]] = names.get(row["name"], False) or row["origin"] != "previous"
    active = sorted((name for name, alive in names.items() if alive), key=_expiry)
    retired = sorted((name for name, alive in names.items() if not alive), key=_expiry, reverse=True)
    return active + retired


def _expiry(name: str) -> tuple[int, int, str]:
    match = _EXPIRY.search(name)
    return (int(match.group(2)), int(match.group(1)), name) if match else (99, 99, name)


def _series_item(row: dict) -> dict:
    live = row["origin"] != "previous"
    return {"instrument": row["name"], "instrument_type": row["instrument_type"], "timeframe": row["interval"],
            "status": "active" if live else "retired", "first_open_time": row["first_open"],
            "last_open_time": row["last_open"], "last_close": row["last_close"],
            "readiness": row["readiness"] if live else None,
            "_links": views.links(self=series_href(row["name"], row["interval"]))}


def _order(row: dict) -> tuple[str, int]:
    return row["name"].upper(), tf_period_minutes(row["interval"])


# --- параметры -----------------------------------------------------------------------

def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if values is None:
        return None
    if len(values) != 1:
        raise CandlesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", f"Параметр {key} — одно значение.")
    return values[0].strip()


def _moment(query: dict[str, list[str]], key: str) -> pd.Timestamp | None:
    """Момент ISO 8601 с зоной → UTC, округлённый вверх до секунды.

    Свечи открываются на целых минутах, поэтому сравнение с округлённой
    границей даёт то же окно, а в базе время — текст без долей секунды.
    """
    raw = _one(query, key)
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(raw.replace(" ", "+"))
    except ValueError:
        value = None
    if value is None or value.tzinfo is None:
        raise CandlesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST",
                           f"{key} — момент ISO 8601 с зоной, например 2026-10-30T06:00:00Z.")
    return _ceil_second(value)


def _ceil_second(value: datetime) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert("UTC").ceil("s")


def _limit(query: dict[str, list[str]]) -> int:
    raw = _one(query, "limit")
    if raw is None:
        return LIMIT_DEFAULT
    if not raw.isdigit() or not 1 <= int(raw) <= LIMIT_MAX:
        raise CandlesError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", f"limit — целое число от 1 до {LIMIT_MAX}.")
    return int(raw)


def _text(value: pd.Timestamp) -> str:
    return value.isoformat().replace("+00:00", "Z")
