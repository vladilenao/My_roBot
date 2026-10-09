"""Read-only сборка уведомляющих фактов у владельца lifecycle.

Не является каналом: получает SQLite и уже доступный рыночный контекст от
менеджера; renderer/Telegram не обращаются сюда и не читают торговый журнал.
"""
from __future__ import annotations

import json
from decimal import Decimal

from src.events.visual import VisualSnapshot, bounded_history, market_snapshot, utc
from src.trade_management.profiles.rules import allocate_target_quantities
from src.trade_management.models import TargetPlan


def plan_data(plan, quantity):
    economics = plan.economics
    allocations = {} if economics is None else economics.target_quantities
    if not allocations and quantity > 0 and plan.targets:
        allocation = allocate_target_quantities(quantity, plan.targets,
            retain_remainder_for_trailing=plan.profile.name == "atr_trend")
        allocations = {t.target_id: t.quantity for t in allocation.targets}
    return {"entry": plan.reference_entry, "stop": plan.stop_price, "quantity": quantity,
            "targets": tuple({"id": t.target_id, "number": i + 1, "price": t.price,
                              "quantity": allocations.get(t.target_id, 0)} for i, t in enumerate(plan.targets)),
            "trailing_quantity": max(0, quantity - sum(allocations.values())),
            "algorithm_version": plan.algorithm_version,
            **({} if economics is None else {"risk_amount": economics.risk_amount, "reward_amount": economics.reward_amount,
                                             "costs_amount": economics.costs_amount, "net_reward_amount": economics.net_reward_amount,
                                             "fixed_reward_amount": economics.fixed_reward_amount}),
            "requested_quantity": plan.requested_quantity}


def quantity_context(instrument=None, meta=None):
    share = getattr(instrument, "instrument_type", "") == "share"
    result = {"unit": "lot" if share else "contract"}
    if meta is not None and meta.price_step > 0:
        result["price_step"] = Decimal(str(meta.price_step))
        if share:
            factor = Decimal(str(meta.step_cost)) / Decimal(str(meta.price_step))
            if factor == factor.to_integral_value() and factor > 0:
                result["lot_size"] = int(factor)
    return result


def signal_snapshot(plan, quantity, instrument, frame=None, as_of=None, meta=None, diagnostics=None):
    as_of = utc(as_of or plan.created_at)
    name = getattr(instrument, "short_name", None) or (
        instrument.ticker if getattr(instrument, "instrument_type", "") == "share" else "контракт не указан")
    data = plan_data(plan, quantity)
    if diagnostics:
        for key in ("requested_quantity", "limiting_constraint"):
            if diagnostics.get(key) is not None:
                data[key] = diagnostics[key]
    return VisualSnapshot({"version": 1, "trade_id": plan.trade_id, "event_key": "signal:" + plan.signal_id,
                           "sequence": 0, "revision": 0, "as_of": as_of.isoformat(), "instrument": name,
                           "side": plan.side, "timeframe": plan.timeframe, **quantity_context(instrument, meta),
                           "plan": data, "state": {"phase": "ENTRY_PENDING", "quantity": 0, "targets": ()},
                           "fills": (), "fill_groups": (), "stops": (),
                           "market": market_snapshot(frame, plan.timeframe, as_of),
                           "financial": {"units": "RUB", "fees_known": False, "fees_source": "unknown"}})


def _rows(connection, sql, args):
    cursor = connection.execute(sql, args)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def lifecycle_snapshot(connection, event, *, instrument=None, meta=None, market=None):
    """Все SELECT — из одного read transaction менеджера после фиксации факта."""
    row = _rows(connection,
        "SELECT t.*,p.quantity,p.average_price,p.realized_pnl,p.fees,s.confirmed_stop "
        "FROM trades t JOIN positions p USING(trade_id) JOIN protection s USING(trade_id) WHERE t.trade_id=?",
        (event.trade_id,))[0]
    plan = json.loads(row["plan_json"])
    economics = plan.get("economics") or {}
    target_numbers = {t["target_id"]: i + 1 for i, t in enumerate(plan.get("targets", ())) }
    targets = _rows(connection, "SELECT * FROM targets WHERE trade_id=? ORDER BY target_index", (event.trade_id,))
    current_targets = tuple({"id": t["target_id"], "number": target_numbers.get(t["target_id"], t["target_index"] + 1),
                             "price": Decimal(t["price"]), "quantity": t["planned_quantity"], "filled": t["filled_quantity"]}
                            for t in targets)
    fills = []
    fee_sources = set()
    for fill in _rows(connection,
        "SELECT f.*,o.action_type FROM fills f JOIN orders o USING(order_id) WHERE f.trade_id=? ORDER BY f.executed_at,f.rowid",
        (event.trade_id,)):
        action, _, target_id = fill["action_type"].partition(":")
        role = f"ЦЕЛЬ{target_numbers[target_id]}" if action == "TARGET" and target_id in target_numbers else action
        side = row["side"] if action in {"OPEN", "ADD"} else ("SELL" if row["side"] == "BUY" else "BUY")
        fills.append({"key": fill["execution_id"], "time": fill["executed_at"], "role": role,
                      "side": side, "quantity": fill["quantity"], "price": Decimal(fill["price"])})
        fee_sources.add(fill["fee_source"])
    stops = []
    previous = None
    # MOVESTOP requested_price + durable ACK уже достаточны для восстановления.
    # next-bar acceptance не является активацией защиты.
    for fact in _rows(connection,
        "SELECT e.event_id,e.occurred_at,e.payload_json,o.action_type,o.requested_price,b.payload_json AS command_json "
        "FROM events e JOIN orders o USING(order_id) JOIN outbox b ON b.command_id=o.command_id "
        "WHERE e.trade_id=? AND ((o.action_type='MOVESTOP' AND e.event_type='ACK') OR "
        "e.event_type IN ('FILL','PARTIAL')) ORDER BY e.event_seq", (event.trade_id,)):
        body = json.loads(fact["payload_json"])
        if body.get("reason") == "next-bar":
            continue
        confirmation = body.get("confirmed_stop", {})
        if fact["action_type"] != "MOVESTOP" and not confirmation:
            continue
        if confirmation.get("old") is not None:
            previous = Decimal(confirmation["old"])
        elif previous is None:
            previous = Decimal(plan["stop_price"])
        price = Decimal(confirmation["new"] if confirmation else fact["requested_price"])
        stops.append({"key": fact["event_id"], "time": fact["occurred_at"], "old": previous,
                      "new": price, "reason": json.loads(fact["command_json"])["reason"] if fact["action_type"] == "MOVESTOP" else "entry-protection"})
        previous = price
    market = market or {"candles": (), "limited": False, "gaps": False,
                        "history_limited": False, "stop_history_limited": False}
    detailed, groups, stops, market = bounded_history(fills, stops, market)
    initial_targets = tuple({"id": t["target_id"], "number": i + 1, "price": Decimal(t["price"]),
                             "quantity": economics.get("target_quantities", {}).get(t["target_id"], 0)}
                            for i, t in enumerate(plan.get("targets", ())))
    quantity = economics.get("quantity")
    if quantity is None:
        quantity = connection.execute("SELECT COALESCE(SUM(quantity),0) FROM orders WHERE trade_id=? AND action_type='OPEN'",
                                      (event.trade_id,)).fetchone()[0]
    if not economics.get("target_quantities") and plan.get("targets") and quantity > 0:
        legacy_targets = tuple(TargetPlan(t["target_id"], Decimal(t["price"]), Decimal(t["share"])) for t in plan["targets"])
        allocated = allocate_target_quantities(quantity, legacy_targets,
            retain_remainder_for_trailing=json.loads(row["profile_json"])["name"] == "atr_trend")
        by_id = {t.target_id: t.quantity for t in allocated.targets}
        initial_targets = tuple({**t, "quantity": by_id.get(t["id"], 0)} for t in initial_targets)
    original = {"entry": Decimal(plan["reference_entry"]), "stop": Decimal(plan["stop_price"]), "quantity": quantity,
                "targets": initial_targets, "trailing_quantity": max(0, quantity - sum(t["quantity"] for t in initial_targets)),
                "algorithm_version": plan.get("algorithm_version", "legacy-v1"),
                **{k: Decimal(str(economics[k])) for k in ("risk_amount", "reward_amount", "costs_amount", "fixed_reward_amount")
                   if economics.get(k) is not None}, "requested_quantity": plan.get("requested_quantity")}
    if economics.get("reward_amount") is not None:
        original["net_reward_amount"] = Decimal(economics["reward_amount"]) - Decimal(economics["costs_amount"])
    financial = {"gross": Decimal(row["realized_pnl"]), "fees": Decimal(row["fees"]),
                 "units": "RUB" if row["price_step"] is not None and row["step_cost"] is not None else "RAW",
                 "fees_known": bool(fee_sources) and "unknown" not in fee_sources,
                 "fees_source": next(iter(fee_sources)) if len(fee_sources) == 1 else "mixed" if fee_sources else "unknown"}
    if financial["units"] == "RUB" and financial["fees_known"]:
        financial["net"] = financial["gross"] - financial["fees"]
    sequence = connection.execute("SELECT event_seq FROM events WHERE event_id=?", (event.execution_id,)).fetchone()[0]
    phase = row["phase"]
    # A request refusal is not a refusal of an already filled position.
    if row["quantity"] > 0 and phase in {"CANCELLED", "REJECTED"}:
        phase = "OPEN"
    return VisualSnapshot({"version": 1, "trade_id": event.trade_id, "event_key": event.execution_id,
                           "sequence": sequence, "revision": row["state_revision"], "as_of": utc(event.timestamp).isoformat(),
                           "instrument": getattr(instrument, "short_name", None) or "контракт не указан",
                           "side": row["side"], "timeframe": plan.get("timeframe", ""), **quantity_context(instrument, meta),
                           "price_step": row["price_step"], "plan": original,
                           "state": {"phase": phase, "quantity": row["quantity"], "average_entry": row["average_price"],
                                     "stop": row["confirmed_stop"], "targets": current_targets},
                           "fills": detailed, "fill_groups": groups, "stops": stops, "market": market, "financial": financial})
