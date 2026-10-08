"""Явно синтетический сценарий одобренного оформления, без HTTP и брокера."""
from datetime import datetime, timedelta
from decimal import Decimal

from src.events.event import Event
from src.events.types import EventType
from src.events.visual import VisualSnapshot


def demo_event(stage="plan", *, side="BUY", trade_id="demo", quantity=2, candles=True):
    times = {"plan": datetime(2026, 10, 6, 7, 30), "entry": datetime(2026, 10, 6, 7, 30),
             "tp1": datetime(2026, 10, 6, 8), "stop": datetime(2026, 10, 6, 8, 15), "final": datetime(2026, 10, 6, 8, 45)}
    stages = ["plan", "entry", "tp1", "stop", "final"]
    index = stages.index(stage)
    as_of = times[stage]
    def p(value):
        value = Decimal(str(value))
        return value if side == "BUY" else Decimal(600) - value
    q1 = (quantity + 1) // 2
    q2 = quantity - q1
    targets = [{"id": "target1", "number": 1, "price": p("303.40"), "quantity": q1},
               {"id": "target2", "number": 2, "price": p("306.40"), "quantity": q2}]
    fills = []
    if index >= 1:
        fills.append({"key": "entry", "time": times["entry"].isoformat(), "role": "OPEN", "side": side,
                      "quantity": quantity, "price": p("300.00")})
    if index >= 2:
        fills.append({"key": "tp1", "time": times["tp1"].isoformat(), "role": "ЦЕЛЬ1", "side": "SELL" if side == "BUY" else "BUY",
                      "quantity": q1, "price": p("303.40")})
    if index == 4 and q2:
        fills.append({"key": "final", "time": times["final"].isoformat(), "role": "ЦЕЛЬ2", "side": "SELL" if side == "BUY" else "BUY",
                      "quantity": q2, "price": p("306.40")})
    stop = p("297.25") if index >= 3 else p("297.00")
    stops = [{"key": "stop", "time": times["stop"].isoformat(), "old": p("297.00"), "new": stop,
              "reason": "cost-aware-break-even"}] if index >= 3 else []
    remaining = 0 if index in {0, 4} else quantity - q1 if index >= 2 else quantity
    current = [{**t, "filled": t["quantity"] if index == 4 or index >= 2 and t["number"] == 1 else 0} for t in targets]
    closes = [297, 297.3, 297.1, 297.6, 297.9, 297.7, 298.1, 298.5, 298.3,
              298.8, 299.1, 298.9, 299.3, 299.6, 299.4, 299.8, 300, 300.5, 302, 303.5, 304.7, 305.3, 306.4]
    bars = []
    start = datetime(2026, 10, 6, 3, 15)
    for i, close in enumerate(closes):
        stamp = start + timedelta(minutes=15*i)
        if stamp + timedelta(minutes=15) > as_of:
            continue
        opening = closes[i-1] if i else close - .2
        opening, close = p(opening), p(close)
        bars.append({"time": stamp.isoformat(), "open": opening, "close": close,
                     "low": min(opening, close) - Decimal(".22"), "high": max(opening, close) + Decimal(".24")})
    gross = Decimal("34") * q1 if index >= 2 else Decimal(0)
    if index == 4:
        gross += Decimal("64") * q2
    fees = Decimal("1.5") * (quantity + (q1 if index >= 2 else 0) + (q2 if index == 4 else 0)) if index else Decimal(0)
    visual = VisualSnapshot({"version": 1, "trade_id": trade_id, "event_key": stage, "sequence": index, "revision": index,
                             "as_of": as_of.isoformat(), "instrument": "SBER", "side": side, "timeframe": "15m", "unit": "lot",
                             "lot_size": 10, "price_step": Decimal(".01"),
                             "plan": {"entry": p("300.00"), "stop": p("297.00"), "quantity": quantity, "targets": targets,
                                      "trailing_quantity": 0, "algorithm_version": "economics-v2", "risk_amount": Decimal(30)*quantity,
                                      "costs_amount": Decimal(4)*quantity, "reward_amount": Decimal(34)*q1+Decimal(64)*q2,
                                      "net_reward_amount": Decimal(34)*q1+Decimal(64)*q2-Decimal(4)*quantity},
                             "state": {"phase": "ENTRY_PENDING" if not index else "CLOSED" if index == 4 else "REDUCING" if index >= 2 else "OPEN",
                                       "quantity": remaining, "average_entry": p("300.00") if remaining else None,
                                       "stop": stop if index else None, "targets": current if index else ()},
                             "fills": fills, "fill_groups": (), "stops": stops,
                             "market": {"candles": bars if candles else (), "limited": False, "gaps": False,
                                        "history_limited": False, "stop_history_limited": False},
                             "financial": {"gross": gross, "fees": fees, "net": gross-fees if index else None,
                                           "units": "RUB", "fees_known": bool(index), "fees_source": "broker" if index else "unknown"}})
    if not index:
        return Event.signal("SBER", side=side, quantity=quantity, entry=p(300), stop=p(297), trade_id=trade_id, visual=visual)
    event_type = {"entry": EventType.TRADE_OPENED, "tp1": EventType.TARGET_HIT,
                  "stop": EventType.STOP_MOVED, "final": EventType.TARGET_HIT}[stage]
    return Event.broker_event(event_type, trade_id=trade_id, instrument="SBER", timeframe="15m", bar_time=as_of,
                              quantity=fills[-1]["quantity"], price=fills[-1]["price"], status="ack" if stage == "stop" else "fill", visual=visual)
