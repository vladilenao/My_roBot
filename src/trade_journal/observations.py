"""Durable, causal price observations of an economics-v2 holding interval."""

from datetime import datetime, timedelta
from decimal import Decimal
import json

from src.scheduler.clock import as_aware
from src.scheduler.timing import tf_period_minutes


def record_fill_price(connection, event, instrument_id):
    """An exact execution price is owned; its full OHLC range is not implied."""
    stamp = as_aware(event.timestamp).isoformat()
    connection.execute(
        "INSERT OR IGNORE INTO trade_market_observations "
        "(trade_id,instrument_id,timeframe,bar_id,observed_price,owned_from,owned_to,coverage,observed_at) "
        "VALUES(?,?,'execution',?,?,?,?,'partial',?)",
        (event.trade_id, instrument_id, f"fill:{event.execution_id}", str(event.price), stamp, stamp, stamp),
    )
    connection.execute("UPDATE trade_measurements SET coverage='partial' WHERE trade_id=?", (event.trade_id,))


def observe_bar(connection, instrument_id, bar_time, prices, *, timeframe="1m", gap_before=False):
    """Record only guaranteed ownership, after all executions of this bar commit.

    Closed base bars use [open, low, high, close]. With no intrabar path, an entry
    or final exit inside a bar retains exact fill prices and marks partial
    coverage. An exit on open cannot acquire that bar's later extrema.
    """
    start = as_aware(bar_time)
    end = start+timedelta(minutes=tf_period_minutes(timeframe))
    identity = start.isoformat()
    open_price, low, high, close = tuple(Decimal(str(p)) for p in prices)
    if not all(p.is_finite() and p > 0 for p in (open_price, low, high, close)) or low > high:
        raise ValueError("invalid observation OHLC")
    count = 0
    trades = connection.execute("SELECT trade_id,plan_json FROM trades WHERE instrument_id=?", (instrument_id,)).fetchall()
    for trade_id, payload in trades:
        if json.loads(payload).get("algorithm_version") != "economics-v2":
            continue
        if connection.execute("SELECT 1 FROM trade_market_observations WHERE trade_id=? AND instrument_id=? AND timeframe=? AND bar_id=?",
                              (trade_id, instrument_id, timeframe, identity)).fetchone():
            continue
        facts = connection.execute(
            "SELECT f.quantity,f.price,f.executed_at,o.action_type FROM fills f JOIN orders o ON o.order_id=f.order_id "
            "WHERE f.trade_id=? ORDER BY f.executed_at,f.rowid", (trade_id,)).fetchall()
        before, during = 0, []
        first_entry = None
        for quantity, price, stamp, action in facts:
            timestamp = as_aware(datetime.fromisoformat(stamp))
            kind = action.split(":", 1)[0]
            increasing = kind in {"OPEN", "ADD"}
            if increasing and first_entry is None:
                first_entry = timestamp
            if timestamp < start:
                before += quantity if increasing else -quantity
            elif timestamp < end:
                during.append((quantity, Decimal(price), timestamp, kind, increasing))
        if before <= 0 and not any(f[4] for f in during):
            continue
        after = before+sum(f[0] if f[4] else -f[0] for f in during)
        entry_on_open = before > 0 or bool(during and during[0][4] and during[0][2] == start and during[0][1] == open_price)
        last_exit = next((f for f in reversed(during) if not f[4]), None)
        exit_on_open = bool(after == 0 and last_exit and last_exit[2] == start
                            and last_exit[1] == open_price and last_exit[3] in {"CLOSE", "REDUCE", "STOP"})
        full_bar = entry_on_open and after > 0
        coverage = "complete" if full_bar or (before > 0 and exit_on_open) else "partial"
        previous = connection.execute(
            "SELECT bar_id,owned_to FROM trade_market_observations WHERE trade_id=? AND timeframe=? ORDER BY bar_id DESC LIMIT 1",
            (trade_id, timeframe),
        ).fetchone()
        missing_interval = (previous is not None and start > as_aware(datetime.fromisoformat(previous[0]))+
                            timedelta(minutes=tf_period_minutes(timeframe)))
        if previous is None and first_entry is not None and first_entry < start:
            missing_interval = True
        if gap_before or missing_interval:
            coverage = "partial"
        connection.execute(
            "INSERT INTO trade_market_observations "
            "(trade_id,instrument_id,timeframe,bar_id,low,high,observed_price,owned_from,owned_to,coverage,observed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, instrument_id, timeframe, identity, str(low) if full_bar else None,
             str(high) if full_bar else None, str(close) if full_bar else str(open_price) if exit_on_open else None,
             identity if entry_on_open else None, end.isoformat() if full_bar else identity if exit_on_open else None,
             coverage, end.isoformat()),
        )
        partial = connection.execute("SELECT 1 FROM trade_market_observations WHERE trade_id=? AND timeframe!='execution' AND coverage='partial' LIMIT 1",
                                     (trade_id,)).fetchone()
        connection.execute("UPDATE trade_measurements SET coverage=?,updated_at=? WHERE trade_id=?",
                           ("partial" if partial else "complete", end.isoformat(), trade_id))
        count += 1
    return count
