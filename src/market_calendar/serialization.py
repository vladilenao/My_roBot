"""Stable representation of broker schedule days for versioning."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
from typing import Any


def _value(value: Any) -> Any:
    if type(value) is object:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds") + "Z"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    annotations = getattr(type(value), "__annotations__", None)
    if annotations is not None:
        return {name: _value(getattr(value, name, None)) for name in annotations}
    if hasattr(value, "name"):
        return value.name
    return value


def canonical_day_json(day: object) -> str:
    """Serialize every named SDK field with UTC-normalized timestamps."""
    return json.dumps(_value(day), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def day_version(day: object) -> str:
    """Return the content version used by stored schedule-day observations."""
    return sha256(canonical_day_json(day).encode("utf-8")).hexdigest()
