from datetime import datetime, timedelta, timezone

from src.market_calendar import canonical_day_json, day_version


def test_sdk_schedule_models_expose_complete_calendar_contract():
    from t_tech.invest.schemas import (
        GetTradingStatusResponse,
        TradingDay,
        TradingInterval,
        TradingSchedulesResponse,
    )

    assert set(TradingSchedulesResponse.__annotations__) == {"exchanges"}
    assert set(TradingDay.__annotations__) == {
        "date", "is_trading_day", "start_time", "end_time",
        "opening_auction_start_time", "opening_auction_end_time",
        "closing_auction_start_time", "closing_auction_end_time",
        "evening_opening_auction_start_time", "evening_start_time", "evening_end_time",
        "clearing_start_time", "clearing_end_time", "premarket_start_time", "premarket_end_time",
        "intervals",
    }
    assert set(TradingInterval.__annotations__) == {"type", "interval"}
    assert set(GetTradingStatusResponse.__annotations__) == {
        "figi", "trading_status", "limit_order_available_flag", "market_order_available_flag",
        "api_trade_available_flag", "instrument_uid", "bestprice_order_available_flag", "only_best_price",
    }


def test_schedule_day_version_is_stable_for_field_order_and_normalizes_utc():
    from t_tech.invest.schemas import TimeInterval, TradingDay, TradingInterval

    stamp = datetime(2026, 10, 12, 10, tzinfo=timezone(timedelta(hours=3)))
    interval = TradingInterval(type="main", interval=TimeInterval(start_ts=stamp, end_ts=stamp + timedelta(hours=1)))
    day = TradingDay(date=stamp, is_trading_day=True, start_time=stamp, end_time=stamp + timedelta(hours=10), intervals=[interval])

    text = canonical_day_json(day)

    assert '"date":"2026-10-12T07:00:00.000000Z"' in text
    assert day_version(day) == day_version(day)
