import json
from datetime import datetime, timezone
from pathlib import Path

from src.market_calendar import CANDLE_RULES, candle_rule
from src.scheduler.timing import CandleScheduler

FIXTURE = Path(__file__).parent / "fixtures" / "candle_boundaries_20261013.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _naive(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)


def test_every_native_timeframe_has_a_confirmed_rule():
    for timeframe in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"):
        rule = candle_rule(timeframe)
        assert rule.timeframe == timeframe
        assert rule.rule_confirmed is True
        assert rule.evidence
    assert set(CANDLE_RULES) == {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"}


def test_broker_candle_boundaries_match_utc_grid():
    data = _load()
    for instrument in data.values():
        for timeframe, rows in instrument["timeframes"].items():
            grid = CandleScheduler(timeframe)
            for row in rows:
                opened = _naive(row["open_utc"])
                assert grid.current_candle_start(opened) == opened, (timeframe, row["open_utc"])


def test_scheduler_bar_close_uses_period_not_last_trade():
    grid = CandleScheduler("1h")
    opened = _naive("2026-10-13T20:00:00+00:00")
    assert grid.bar_close(opened) == _naive("2026-10-13T21:00:00+00:00")
