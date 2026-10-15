"""Синтетические 1m свечи и события ClusterSource для тестов «Кластеров».

История до дня сигнала устроена так, что каждое 15-минутное движение по
модулю ровно ``SIGMA``: цена чередует два уровня блоками по 15 минут. День
сигнала воспроизводит закрытия BR-11.26 15.10.2026 (design D5).
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from src.clusters.store import StoredEvent

SIGMA = 0.001282  # 12,82 б.п.
T = datetime(2026, 10, 15, 13, 50, tzinfo=timezone.utc)
DAY = date(2026, 10, 15)


def history(days: int = 10, end: date = DAY, base: float = 100.0) -> list[dict]:
    """``days`` торговых дней до ``end``: 07:00–15:00 UTC, два уровня блоками по 15 минут."""
    rows = []
    day = end - timedelta(days=1)
    added = 0
    while added < days:
        if day.weekday() < 5:
            start = datetime(day.year, day.month, day.day, 7, 0)
            for minute in range(8 * 60):
                level = base * (math.exp(SIGMA) if (minute // 15) % 2 else 1.0)
                rows.append({"datetime": start + timedelta(minutes=minute), "high": level, "low": level, "close": level})
            added += 1
        day -= timedelta(days=1)
    return rows


def signal_day(closes: dict[str, float] | None = None, base: float = 104.80,
               high: float = 105.20, low: float = 104.06) -> list[dict]:
    """1m свечи 11:30–13:49 UTC дня сигнала; ``closes`` — закрытия по «HH:MM» времени открытия."""
    closes = closes if closes is not None else BR_CLOSES
    rows = []
    start = datetime(2026, 10, 15, 11, 30)
    for minute in range(140):
        moment = start + timedelta(minutes=minute)
        close = closes.get(moment.strftime("%H:%M"), base)
        rows.append({"datetime": moment, "high": close, "low": close, "close": close})
    rows[30]["high"] = high  # 12:00 — внутри 2ч окна до 13:50
    rows[40]["low"] = low  # 12:10
    return rows


# Закрытия BR-11.26 15.10.2026 (UTC, время открытия свечи), по которым считается пример D5.
BR_CLOSES = {
    "13:07": 104.57, "13:09": 104.68, "13:21": 104.93, "13:22": 105.01, "13:24": 105.12,
    "13:34": 104.92, "13:36": 104.92, "13:39": 104.79, "13:49": 104.63,
}


def candles(*parts: list[dict]) -> pd.DataFrame:
    rows = [row for part in parts for row in part]
    return pd.DataFrame(rows, columns=["datetime", "high", "low", "close"])


def event(event_type: str, direction: str, published: datetime, *, secid: str = "BRX6",
          payload: dict | None = None, event_id: str | None = None, category: str = "signal") -> StoredEvent:
    event_id = event_id or f"{event_type}-{direction}-{published.isoformat()}"
    fields = {"category": category, "event_type": event_type, "secid": secid, "direction": direction,
              "event_id": event_id, "signal_id": event_id, "payload": json.dumps(payload or {})}
    return StoredEvent(event_id=event_id, stream_id="0-1", category=category, event_type=event_type,
                       secid=secid, source_ticker="BR_CONTINUOUS", direction=direction, signal_id=event_id,
                       timestamp=published, published_at=published, payload=payload or {}, fields=fields)


def br_events() -> list[StoredEvent]:
    """События BRX6 перед SELL 13:50 UTC 15.10 (design D5)."""
    utc = timezone.utc
    return [
        event("large_aggressor", "long", datetime(2026, 10, 15, 13, 23, 34, 286186, tzinfo=utc),
              payload={"side": "BUY", "size": 1300, "size_unit": "contracts"}),
        event("stacked_imbalance", "long", datetime(2026, 10, 15, 13, 25, 1, 722406, tzinfo=utc),
              payload={"side": "BUY"}),
        event("large_aggressor", "short", datetime(2026, 10, 15, 13, 37, 27, 698296, tzinfo=utc),
              payload={"side": "SELL", "size": 650, "size_unit": "contracts"}),
        event("volume_spike", "short", datetime(2026, 10, 15, 13, 40, 0, 190110, tzinfo=utc),
              payload={"ratio": 9.606, "delta": -2031.0}),
    ]


def stream_fields(event_id: str, *, category: str = "signal", event_type: str = "volume_spike",
                  secid: str = "BRX6", published: str = "2026-10-15T13:40:00.190110Z") -> dict[str, str]:
    """Поля записи Redis Stream, как их пишет ClusterSource."""
    return {
        "category": category, "event_type": event_type, "event_id": event_id, "secid": secid,
        "ticker": "BR_CONTINUOUS", "direction": "short", "signal_id": event_id, "schema_version": "1",
        "source": "cluster_source", "timestamp": "2026-10-15T13:40:00.000000Z", "published_at": published,
        "payload": json.dumps({"ratio": 9.606}),
    }
