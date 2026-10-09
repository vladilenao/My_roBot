"""Версионированный самодостаточный снимок для представления сделки.

Здесь нет форматирования, рендера или IO. Числа цены/денег остаются Decimal
до сериализации; контейнеры отделены от изменяемых источников.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from src.scheduler.timing import CandleScheduler

CANDLE_LIMIT = 80
HISTORY_LIMIT = 64
SCHEMA_ID = "visual-snapshot.json"


def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items() if v is not None})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


def serializable(value):
    if isinstance(value, Mapping):
        return {k: serializable(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def visual_schema():
    text, integer, boolean = {"type": "string"}, {"type": "integer", "minimum": 0}, {"type": "boolean"}
    target = _object({"id": text, "number": integer, "price": text, "quantity": integer, "filled": integer},
                     ("id", "number", "price", "quantity"))
    targets = {"type": "array", "items": target}
    fill = _object({"key": text, "time": text, "role": text, "side": text, "quantity": integer, "price": text},
                   ("key", "time", "role", "side", "quantity", "price"))
    group = _object({"role": text, "side": text, "start": text, "end": text, "quantity": integer,
                     "price": text, "count": integer}, ("role", "side", "start", "end", "quantity", "price", "count"))
    stop = _object({"key": text, "time": text, "old": text, "new": text, "reason": text}, ("key", "time", "new", "reason"))
    candle = _object({k: text for k in ("time", "open", "high", "low", "close")}, ("time", "open", "high", "low", "close"))
    plan = _object({"entry": text, "stop": text, "quantity": integer, "targets": targets,
                    "trailing_quantity": integer, "algorithm_version": text,
                    **{k: text for k in ("risk_amount", "reward_amount", "costs_amount", "net_reward_amount", "fixed_reward_amount")},
                    "requested_quantity": integer, "limiting_constraint": text},
                   ("entry", "stop", "quantity", "targets", "trailing_quantity", "algorithm_version"))
    state = _object({"phase": text, "quantity": integer, "average_entry": text, "stop": text, "targets": targets},
                    ("phase", "quantity", "targets"))
    market = _object({"candles": {"type": "array", "items": candle, "maxItems": CANDLE_LIMIT},
                     "limited": boolean, "gaps": boolean, "history_limited": boolean,
                     "stop_history_limited": boolean, "boundary_stop": text},
                    ("candles", "limited", "gaps", "history_limited", "stop_history_limited"))
    financial = _object({"gross": text, "fees": text, "net": text, "units": text,
                        "fees_source": text, "fees_known": boolean}, ("units", "fees_known", "fees_source"))
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": SCHEMA_ID,
            **_object({"version": {"type": "integer", "enum": [1]}, "trade_id": text, "event_key": text,
                       "sequence": integer, "revision": integer, "as_of": text, "instrument": text,
                       "side": {"type": "string", "enum": ["BUY", "SELL"]}, "timeframe": text,
                       "unit": {"type": "string", "enum": ["lot", "contract"]}, "lot_size": integer,
                       "price_step": text, "plan": plan, "state": state,
                       "fills": {"type": "array", "items": fill, "maxItems": HISTORY_LIMIT},
                       "fill_groups": {"type": "array", "items": group},
                       "stops": {"type": "array", "items": stop, "maxItems": HISTORY_LIMIT},
                       "market": market, "financial": financial},
                      ("version", "trade_id", "event_key", "sequence", "revision", "as_of", "instrument",
                       "side", "timeframe", "unit", "plan", "state", "fills", "fill_groups", "stops", "market", "financial"))}


def validate(value, schema, path="visual"):
    """Небольшой строгий валидатор используемого подмножества JSON Schema."""
    kind = schema.get("type")
    valid = {"object": isinstance(value, Mapping), "array": isinstance(value, (tuple, list)),
             "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "integer": isinstance(value, int) and not isinstance(value, bool)}.get(kind, False)
    if not valid or "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: invalid {kind}")
    if "minimum" in schema and value < schema["minimum"]:
        raise ValueError(f"{path}: below minimum")
    if kind == "object":
        if set(schema.get("required", ())) - set(value) or set(value) - set(schema["properties"]):
            raise ValueError(f"{path}: fields outside schema or missing required fields")
        for key, item in value.items():
            validate(item, schema["properties"][key], f"{path}.{key}")
    if kind == "array":
        if len(value) > schema.get("maxItems", len(value)):
            raise ValueError(f"{path}: too many items")
        for item in value:
            validate(item, schema["items"], path + "[]")


@dataclass(frozen=True)
class VisualSnapshot:
    data: Mapping

    def __post_init__(self):
        detached = freeze(self.data)
        validate(serializable(detached), visual_schema())
        object.__setattr__(self, "data", detached)

    def to_dict(self):
        return serializable(self.data)


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def market_snapshot(frame, timeframe, as_of):
    """Данные только из переданного frame; datetime — начало свечи UTC."""
    result = {"candles": (), "limited": False, "gaps": False,
              "history_limited": False, "stop_history_limited": False}
    if frame is None or not timeframe:
        return result
    grid = CandleScheduler(timeframe)
    as_of = utc(as_of)
    candles = []
    for row in frame.tail(CANDLE_LIMIT + 1).to_dict("records"):
        try:
            time = utc(row["datetime"])
            if grid.bar_close(time) > as_of:
                continue
            prices = {key: Decimal(str(row[key])) for key in ("open", "high", "low", "close")}
            if any(not p.is_finite() or p <= 0 for p in prices.values()):
                continue
            if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
                continue
            candles.append({"time": time.isoformat(), **prices})
        except (KeyError, ValueError, InvalidOperation, TypeError):
            continue
    candles = sorted({c["time"]: c for c in candles}.values(), key=lambda c: c["time"])
    result["limited"] = len(frame) > CANDLE_LIMIT
    result["candles"] = tuple(candles[-CANDLE_LIMIT:])
    result["gaps"] = any(utc(b["time"]) != utc(grid.bar_close(utc(a["time"]))) for a, b in zip(candles, candles[1:]))
    return result


def bounded_history(fills, stops, market):
    """Сворачивает ранние факты, не обрезая количество или финансовый итог."""
    groups = {}
    for fill in fills[:-HISTORY_LIMIT]:
        key = fill["role"], fill["side"]
        group = groups.setdefault(key, {"role": key[0], "side": key[1], "start": fill["time"],
                                        "end": fill["time"], "quantity": 0, "price": Decimal(0), "count": 0})
        group["end"] = fill["time"]
        group["quantity"] += fill["quantity"]
        group["price"] += Decimal(str(fill["price"])) * fill["quantity"]
        group["count"] += 1
    for group in groups.values():
        group["price"] /= group["quantity"]
    market = dict(market, history_limited=len(fills) > HISTORY_LIMIT,
                  stop_history_limited=len(stops) > HISTORY_LIMIT)
    candles = market.get("candles", ())
    if candles:
        earlier = [s for s in stops if utc(s["time"]) <= utc(candles[0]["time"])]
        if earlier:
            market["boundary_stop"] = earlier[-1]["new"]
    return tuple(fills[-HISTORY_LIMIT:]), tuple(groups.values()), tuple(stops[-HISTORY_LIMIT:]), market
