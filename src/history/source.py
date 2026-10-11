"""Идентичность диагностического и рабочего источника одного прогона."""

from hashlib import sha256


def source_identity(provider) -> tuple[str, int | None]:
    """Идентичность не раскрывает URL с возможными credentials."""
    url = getattr(provider, "base_url", None)
    name = url if isinstance(url, str) else type(provider).__module__+"."+type(provider).__qualname__
    snapshot = getattr(provider, "snapshot_generation", None)
    if type(snapshot) is not int:
        snapshot = getattr(provider, "_snapshot_generation", None)
    return sha256(name.encode()).hexdigest(), snapshot if type(snapshot) is int else None
