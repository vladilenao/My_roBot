from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from src.broker.port import ExecutionEvent, ExecutionStatus
from src.instruments import Instrument
from src.portfolio.models import ContractMeta
from src.trade_journal.storage import Storage
from src.trade_management.actions import MoveStop, OpenTrade, ReduceTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import CostSnapshot, PlanEconomics, ProfileSnapshot, TargetPlan, TradePlan

D = Decimal
NOW = datetime(2026, 10, 6, 7, 30)


class Broker:
    def contract_for(self, ticker):
        return ContractMeta(ticker, .01, .1, 0, 0)


def setup(storage, facts):
    manager = TradeManager(storage, Broker(), initial_balance=D(100000), max_qty=10,
                           execution_observer=lambda event, data: facts.append((event, data)), clock=lambda: NOW)
    manager.configure_visual_context([Instrument("Сбер", "SBER", "share", "SBER")])
    plan = TradePlan("trade", "assignment", "SBER", "BUY", "signal", D(300), D(297),
                     (TargetPlan("first", D("303.4"), D(".5")), TargetPlan("second", D("306.4"), D(".5"))),
                     ProfileSnapshot("levels_rr", "1", {}), NOW, timeframe="15m", algorithm_version="economics-v2",
                     cost_snapshot=CostSnapshot(), entry_order_type="limit", price_step=D(".01"),
                     economics=PlanEconomics(2, D(60), D(98), D(8), D("1.5"), target_quantities={"first": 1, "second": 1}))
    action = OpenTrade("entry", "trade", 0, "entry", 2, "limit", D(300))
    assert manager.submit_plan(plan, action, price_step=D(".01"), step_cost=D(".1"))
    return manager, action


def fact(manager, action, key, status, quantity=0, price=None, fee=None, minute=0, reason="confirmed"):
    event = ExecutionEvent(key, action.command_id, action.command_id, action.trade_id, status, quantity,
                           None if price is None else D(str(price)), None if fee is None else D(str(fee)),
                           NOW + timedelta(minutes=minute), reason)
    assert manager.consume(event)
    return event


def test_actual_snapshot_money_target_identity_and_effective_stop_survive_restart(tmp_path):
    facts = []
    with Storage(tmp_path / "trade.db") as storage:
        manager, entry = setup(storage, facts)
        filled = fact(manager, entry, "entry-fill", ExecutionStatus.FILL, 2, 300, 3)
        assert not manager.consume(filled)
        state = storage.load_trade("trade").state
        target = ReduceTrade("target", "trade", state.state_revision, "target", 1, "first")
        manager.submit_action(target)
        fact(manager, target, "tp1", ExecutionStatus.FILL, 1, "303.4", "1.5", minute=30)
        move = MoveStop("move", "trade", storage.load_trade("trade").state.state_revision, "cost-aware-break-even", D("297.25"))
        manager.submit_action(move)
        fact(manager, move, "accepted", ExecutionStatus.ACK, minute=31, reason="next-bar")
        assert len(facts) == 2
        assert storage.load_trade("trade").state.confirmed_stop == 297
        active = fact(manager, move, "stop-active", ExecutionStatus.ACK, minute=45)
        assert len(facts) == 3
        snapshot = facts[-1][1]["visual"].to_dict()
        assert snapshot["stops"][-1]["old"] == "297"
        assert snapshot["stops"][-1]["new"] == "297.25"
        assert D(snapshot["financial"]["gross"]) == 34
        assert D(snapshot["financial"]["net"]) == D("29.5")
        assert snapshot["unit"] == "lot" and snapshot["lot_size"] == 10
        restored = TradeManager(storage, Broker(), execution_observer=lambda event, data: facts.append((event, data)), clock=lambda: NOW)
        restored.configure_visual_context([Instrument("Сбер", "SBER", "share", "SBER")])
        assert not restored.consume(active)
        target2 = ReduceTrade("target2", "trade", storage.load_trade("trade").state.state_revision, "target", 1, "second")
        restored.submit_action(target2)
        fact(restored, target2, "tp2", ExecutionStatus.FILL, 1, "306.4", "1.5", minute=75)
        final = facts[-1][1]["visual"].to_dict()
        assert final["state"]["phase"] == "CLOSED" and final["state"]["quantity"] == 0
        assert final["fills"][-1]["role"] == "ЦЕЛЬ2"
        assert D(final["financial"]["gross"]) == 98
        assert D(final["financial"]["fees"]) == 6
        assert D(final["financial"]["net"]) == 92
        assert final["stops"][-1] == snapshot["stops"][-1]


def test_cancel_partial_entry_and_reject_stop_do_not_claim_closed_position(tmp_path):
    facts = []
    with Storage(tmp_path / "trade.db") as storage:
        manager, entry = setup(storage, facts)
        fact(manager, entry, "partial", ExecutionStatus.PARTIAL, 1, 300, "1.5")
        fact(manager, entry, "cancel-rest", ExecutionStatus.CANCEL, minute=1)
        assert facts[-1][1]["visual"].data["state"]["quantity"] == 1
        assert facts[-1][1]["visual"].data["state"]["phase"] == "OPEN"
        move = MoveStop("move", "trade", storage.load_trade("trade").state.state_revision, "trailing", D(298))
        manager.submit_action(move)
        rejected = fact(manager, move, "reject-stop", ExecutionStatus.REJECT, minute=2)
        snapshot = facts[-1][1]["visual"].data
        assert snapshot["state"]["quantity"] == 1 and snapshot["state"]["stop"] == "297"
        assert all(s["reason"] == "entry-protection" for s in snapshot["stops"])
        assert not manager.consume(rejected)


def test_old_facts_remain_immutable_when_new_fills_apply(tmp_path):
    facts = []
    with Storage(tmp_path / "trade.db") as storage:
        manager, entry = setup(storage, facts)
        partial = fact(manager, entry, "partial", ExecutionStatus.PARTIAL, 1, 300, "1.5")
        snapshot = facts[-1][1]["visual"].to_dict()
        fact(manager, entry, "rest", ExecutionStatus.FILL, 1, 300, "1.5", minute=1)
        assert facts[0][1]["visual"].to_dict() == snapshot
        assert facts[-1][1]["visual"].data["state"]["quantity"] == 2
        assert not manager.consume(replace(partial))
