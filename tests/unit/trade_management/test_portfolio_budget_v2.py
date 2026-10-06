"""Независимая арифметика общего бюджета через durable admission и fills."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pandas as pd
from types import SimpleNamespace

from src.broker.port import ExecutionEvent, ExecutionStatus, FeeSource
from src.portfolio.models import ContractMeta
from src.trade_journal.storage import ReservationCandidate, Storage
from src.trade_journal.export import CsvExporter
from src.trade_management.actions import AddToTrade, CloseTrade, MoveStop, OpenTrade, ReduceTrade
from src.trade_management.errors import ReservationRejected, RiskStateUnknown
from src.trade_management.manager import TradeManager
from src.trade_management.models import CostSnapshot, PlanEconomics, ProfileSnapshot, TargetPlan, TradePlan

D = Decimal
NOW = datetime(2026, 10, 2, 10, tzinfo=timezone.utc)


class Broker:
    def __init__(self, go=0):
        self.meta = ContractMeta("SBER", 1, 100, go, go)

    def contract_for(self, instrument):
        return self.meta


def plan(identifier="first", *, side="BUY", entry=100, stop=96, costs=None, requested=None):
    return TradePlan(
        identifier, f"assignment:{identifier}", "SBER", side, f"signal:{identifier}",
        D(entry), D(stop), (), ProfileSnapshot("ma_cloud", "1", {}), NOW,
        algorithm_version="economics-v2" if costs is not None else "legacy-v1",
        cost_snapshot=costs, entry_order_type="limit" if costs is not None else "market",
        requested_quantity=requested,
    )


def manager(storage, *, balance=50000, go=0, **kwargs):
    return TradeManager(storage, Broker(go), initial_balance=D(balance), max_qty=10, clock=lambda: NOW, **kwargs)


def submit(manager, p, quantity, *, risk=None, margin=0):
    action = OpenTrade(f"{p.trade_id}:entry", p.trade_id, 0, "entry", quantity,
                       p.entry_order_type, p.reference_entry if p.entry_order_type == "limit" else None)
    reservation = None if risk is None else ReservationCandidate(
        f"reserve:{p.trade_id}", p.trade_id, action.command_id, 0,
        p.assignment_id, p.instrument_id, p.signal_id, D(risk), D(margin),
    )
    kwargs = {} if risk is None else {"reservation": reservation, "risk_budget": D("100000"), "margin_budget": D("100000")}
    assert manager.submit_plan(p, action, price_step=D(1), step_cost=D(100), **kwargs)
    return action


def fill(manager, action, quantity, price=100, *, status=ExecutionStatus.FILL, identifier="fill"):
    event = ExecutionEvent(identifier, action.command_id, action.command_id, action.trade_id,
                           status, quantity, D(price), D(0), NOW, "entry", fee_source=FeeSource.UNKNOWN)
    assert manager.consume(event)
    return event


def test_sizing_includes_roundtrip_costs_and_requested_quantity(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        p = plan(costs=CostSnapshot(D(5), D(10)), requested=3)
        sizing = m._size_open_quantity(p, m._broker.meta)
        assert sizing.quantity == 2  # floor(1000/(400+20))
        assert sizing.risk_amount == D(840)
        assert m._size_open_quantity(replace(p, requested_quantity=1), m._broker.meta).quantity == 1
        assert m._size_open_quantity(replace(p, requested_quantity=0), m._broker.meta).code == "zero-quantity"


def test_margin_selects_a_positive_smaller_quantity(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, balance=20000, go=8000)
        p = plan(stop=99, requested=5, costs=CostSnapshot(D(0), D(0)))
        sizing = m._size_open_quantity(p, m._broker.meta)
        assert sizing.quantity == 2  # risk permits four, margin permits two
        assert sizing.code is None
        submit(m, p, 2, risk=200, margin=16000)
        state = m._budget_state(storage.connection)
        assert state.pending_margin == D(16000)
        assert state.pending_risk == D(200)


def test_sequential_candidates_get_two_then_one_from_the_shared_2_percent(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        first = replace(plan(), requested_quantity=3)
        sizing = m._size_open_quantity(first, m._broker.meta)
        assert sizing.quantity == 2
        submit(m, first, sizing.quantity, risk=sizing.risk_amount)
        second = plan("second", stop="98.5")
        sizing = m._size_open_quantity(second, m._broker.meta)
        assert sizing.quantity == 1
        submit(m, second, 1, risk=150)
        state = m._budget_state(storage.connection)
        assert state.pending_risk == D(950)
        assert state.limit-state.used_risk == D(50)


def test_partial_entry_moves_only_filled_reserve_into_open_risk(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, go=100)
        action = submit(m, plan(costs=CostSnapshot(D(0), D(0))), 2, risk=800, margin=200)
        event = fill(m, action, 1, status=ExecutionStatus.PARTIAL)
        state = m._budget_state(storage.connection)
        assert (state.open_risk, state.pending_risk, state.used_risk) == (D(400), D(400), D(800))
        assert (state.open_margin, state.pending_margin, state.used_margin) == (D(100), D(100), D(200))
        assert not m.consume(event)
        assert m._budget_state(storage.connection).used_risk == D(800)


def test_only_confirmed_profitable_stop_frees_price_risk(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        action = submit(m, plan(costs=CostSnapshot(D("1.5"), D(1))), 2)
        fill(m, action, 2)
        before = storage.load_trade("first")
        stop = MoveStop("move", "first", before.state.state_revision, "profile", D(101))
        m.submit_action(stop, market_close=D(105))
        assert m._budget_state(storage.connection).open_risk == D(804)
        m.consume(ExecutionEvent("ack", "move", "move", "first", ExecutionStatus.ACK, 0, None, D(0), NOW, "stop"))
        assert m._budget_state(storage.connection).open_risk == D(4)
        assert storage.load_trade("first").state.initial_stop_distance == D(4)


def test_equity_updates_budget_without_changing_entry_stop_risk(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        action = submit(m, plan(costs=CostSnapshot(D(0), D(0))), 2)
        fill(m, action, 2)
        assert m.mark_to_market({"SBER": D(110)})
        assert m._budget_state(storage.connection).base == D(50000)
        assert m.mark_to_market({"SBER": D(90)})
        state = m._budget_state(storage.connection)
        assert (state.base, state.limit, state.open_risk) == (D(48000), D(960), D(800))
        assert not m.mark_to_market({})
        with pytest.raises(RiskStateUnknown):
            m._budget_state(storage.connection)


def test_unknown_stop_blocks_increases_but_allows_owned_exit(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        action = submit(m, plan(), 2)
        fill(m, action, 2)
        with storage.transaction() as connection:
            connection.execute("UPDATE protection SET confirmed_stop=NULL WHERE trade_id='first'")
        with pytest.raises(RiskStateUnknown):
            m._size_open_quantity(plan("second"), m._broker.meta)
        state = storage.load_trade("first").state
        assert m.submit_action(CloseTrade("close", "first", state.state_revision, "profile-exit"))


def test_legacy_future_estimate_does_not_rewrite_historical_fees_or_pending(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, commission=D("1.5"), slippage=D(1))
        action = submit(m, plan(), 2)
        fill(m, action, 2)
        payload = storage.connection.execute("SELECT plan_json FROM trades WHERE trade_id='first'").fetchone()[0]
        submit(m, plan("old-pending"), 1, risk=100)
        state = m._budget_state(storage.connection)
        assert state.used_risk == D(904)
        assert state.future_cost_sources == {"first": "legacy-future-estimate"}
        assert storage.connection.execute("SELECT fee,fee_source FROM fills").fetchone() == ("0", "unknown")
        assert storage.connection.execute("SELECT plan_json FROM trades WHERE trade_id='first'").fetchone()[0] == payload
        restarted = manager(storage, commission=D(3), slippage=D(2), portfolio_pct=1)
        assert restarted._budget_state(storage.connection).used_risk == D(908)
        assert restarted._size_open_quantity(plan("new"), restarted._broker.meta).code == "risk-budget"


def test_race_at_reserve_rolls_back_candidate_plan_and_outbox(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        frozen = plan("frozen")
        assert m._size_open_quantity(frozen, m._broker.meta).quantity == 2
        submit(m, plan("competitor"), 1, risk=300)
        with pytest.raises(ReservationRejected, match="risk-budget"):
            submit(m, frozen, 2, risk=800)
        assert storage.connection.execute("SELECT COUNT(*) FROM trades WHERE trade_id='frozen'").fetchone()[0] == 0
        assert storage.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_short_addition_reserves_incremental_resulting_risk_without_double_counting(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, balance=25000)
        action = submit(m, plan(side="SELL", stop=101, costs=CostSnapshot(D(0), D(0))), 2)
        fill(m, action, 2)
        state = storage.load_trade("first").state
        addition = AddToTrade("add", "first", state.state_revision, "signal", 3, "limit", D(98))
        assert m.submit_action(addition)
        payload = json.loads(storage.connection.execute("SELECT payload_json FROM outbox WHERE command_id='add'").fetchone()[0])
        assert payload["quantity"] == 1
        assert payload["requested_quantity"] == 3 and payload["limiting_constraint"] == "risk"
        assert m._budget_state(storage.connection).used_risk == D(500)  # 200 old + 300 reserved


@pytest.mark.parametrize("risk,costs,slippage,reward,code", [
    (80, 40, 20, 160, None),
    (100, 40, 0, 180, "payoff-below-floor"),
    ("4.85", 40, 0, None, "risk-cost-ratio"),
    (80, 40, "20.01", None, "slippage-risk-ratio"),
    (80, 40, 0, "159.92", "payoff-below-floor"),  # 1.499, not displayed 1.50
    (80, 0, 0, 120, None),
    (80, 60, 0, 60, "cost-exceeds-reward"),
])
def test_economic_order_threshold_equality_unknown_total_and_exact_decimal(tmp_path, risk, costs, slippage, reward, code):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        r, c = D(risk), D(costs)
        reward = None if reward is None else D(reward)
        economics = PlanEconomics(1, r, reward, c, None if reward is None else (reward-c)/r, slippage_amount=D(slippage))
        refusal = m._entry_economics_refusal(plan(costs=CostSnapshot(D(0), D(0))), economics, 1, r+c, D(50000))
        assert (None if refusal is None else refusal.rejections[0].code) == code
        assert storage.connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        if code:
            trace = storage.connection.execute("SELECT reason,trade_id FROM calculations").fetchone()
            assert trace == (code, None)


def test_zero_global_percentage_blocks_even_free_addition(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, portfolio_pct=0)
        assert m._size_open_quantity(plan(), m._broker.meta).code == "risk-budget"


def test_previously_accepted_fill_above_reduced_budget_is_applied_without_liquidation(tmp_path, caplog):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        action = submit(m, plan(costs=CostSnapshot(D(0), D(0))), 2, risk=800)
        with storage.transaction() as connection:
            connection.execute("UPDATE account SET balance='25000',equity='25000'")
        fill(m, action, 2)
        assert storage.load_trade("first").state.quantity == 2
        assert m._budget_state(storage.connection).open_risk == D(800)
        assert m._size_open_quantity(plan("second"), m._broker.meta).code == "risk-budget"
        assert storage.connection.execute("SELECT action_type FROM orders").fetchall() == [("OPEN",)]
        assert "превышение 300" in caplog.text
        state = storage.load_trade("first").state
        assert m.submit_action(CloseTrade("profile-exit", "first", state.state_revision, "profile-exit"))


def test_d0_uses_rounded_first_protection_and_survives_add_reduce_restart(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, balance=100000)
        p = replace(plan(costs=CostSnapshot(D(0), D(0))), price_step=D(1))
        action = submit(m, p, 2)
        fill(m, action, 2, price="99.7")
        first = storage.load_trade("first").state
        assert (first.confirmed_stop, first.initial_stop_distance, first.max_quantity) == (D(95), D("4.7"), 2)
        addition = AddToTrade("add", "first", first.state_revision, "signal", 1, "limit", D(102))
        m.submit_action(addition)
        fill(m, addition, 1, price=102, identifier="add-fill")
        enlarged = storage.load_trade("first").state
        assert enlarged.initial_stop_distance == D("4.7") and enlarged.max_quantity == 3
        reduction = ReduceTrade("reduce", "first", enlarged.state_revision, "profile-exit", 1)
        m.submit_action(reduction)
        fill(m, reduction, 1, price=104, identifier="exit-fill")
        restored = manager(storage).restore()[0].state
        assert restored.quantity == 2 and restored.max_quantity == 3
        assert restored.initial_stop_distance == D("4.7")
        row = CsvExporter(storage.connection, tmp_path / "events.csv", tmp_path / "summary.csv")._snapshot()[2][0]
        assert row["initial_risk"] == "1410.00"  # D0 4.7 × V 100 × Qmax 3


def test_d0_uses_existing_confirmed_initial_protection_when_fill_did_not_rebase_it(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        action = submit(m, plan(costs=CostSnapshot(D(0), D(0))), 1)
        with storage.transaction() as connection:
            connection.execute("UPDATE protection SET confirmed_stop='96' WHERE trade_id='first'")
        event = ExecutionEvent("initial", action.command_id, action.command_id, "first", ExecutionStatus.FILL,
                               1, D(102), D(0), NOW, "entry")
        assert m._reducer.apply(event)
        assert storage.load_trade("first").state.initial_stop_distance == D(6)


def test_manager_persists_zero_allocation_trailing_activation_without_target_fill(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, balance=100000)
        p = replace(plan(costs=CostSnapshot(D(0), D(0))),
                    targets=(TargetPlan("tp-1", D(104), D("0.25")), TargetPlan("tp-2", D(108), D("0.25"))),
                    profile=ProfileSnapshot("atr_trend", "1", {"atr_period": 14, "trail_k": 2}))
        action = submit(m, p, 3)
        fill(m, action, 3)
        frame = pd.DataFrame({"datetime": pd.date_range(NOW, periods=21, freq="min"),
                              "open": [100]*21, "low": [99]*21, "high": [101]*20+[105], "close": [100]*20+[104]})
        m.manage(SimpleNamespace(ticker="SBER"), [], frame, timeframe="1m", now=NOW)
        recovered = manager(storage).restore()[0]
        assert recovered.state.trailing_active and recovered.state.adds_disabled
        assert recovered.state.trailing_extreme == 105
        assert not recovered.state.completed_target_ids and recovered.state.quantity == 3
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_legacy_be_uses_saved_cost_input_or_explicit_unknown_not_new_config(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, commission=D(999), slippage=D(999))
        legacy = replace(plan(), economics=PlanEconomics(10, D(4000), D(6000), D(40), D("1.49")))
        submit(m, legacy, 10)
        market = {"entry_cost": D(999), "exit_cost": D(999), "slippage_cost": D(999)}
        m._enrich_owned_market(storage.load_trade("first"), market)
        assert market["legacy_costs_known"]
        assert (market["entry_cost"], market["exit_cost"], market["slippage_cost"]) == (D(4), D(0), D(0))
        submit(m, plan("unknown-costs"), 1)
        m._enrich_owned_market(storage.load_trade("unknown-costs"), market)
        assert not market["legacy_costs_known"] and market["entry_cost"] is None


def test_budget_alert_and_financial_fact_observers_are_idempotent_after_commit(tmp_path):
    alerts, facts = [], []
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, budget_observer=alerts.append, execution_observer=lambda event, details: facts.append((event, details)))
        action = submit(m, plan(costs=CostSnapshot(D(0), D(0))), 3)
        event = fill(m, action, 3)
        assert len(facts) == len(alerts) == 1
        assert facts[0][1]["quantity_remaining"] == 3 and facts[0][1]["gross_pnl"] == 0
        assert alerts[0]["open_risk"] == 1200 and alerts[0]["risk_budget"] == 1000 and alerts[0]["risk_excess"] == 200
        assert not m.consume(event)
        m._record_portfolio_budget()
        assert len(facts) == len(alerts) == 1
        assert storage.connection.execute("SELECT action_type FROM orders").fetchall() == [("OPEN",)]


def test_notification_failure_does_not_undo_execution(tmp_path):
    def unavailable(*args):
        raise RuntimeError("channel unavailable")
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, execution_observer=unavailable)
        action = submit(m, plan(costs=CostSnapshot(D(0), D(0))), 2)
        fill(m, action, 2)
        assert storage.load_trade("first").state.quantity == 2
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_partial_add_counts_one_confirmed_order_and_restores_profile_limit(tmp_path):
    from src.trade_management.profiles.rules import add_quantity
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        action = submit(m, plan(stop=99, costs=CostSnapshot(D(0), D(0))), 1)
        fill(m, action, 1)
        state = storage.load_trade("first").state
        addition = AddToTrade("add", "first", state.state_revision, "signal", 2, "limit", D(101))
        m.submit_action(addition)
        first = fill(m, addition, 1, price=101, status=ExecutionStatus.PARTIAL, identifier="add-first")
        assert storage.load_trade("first").state.add_count == 1
        fill(m, addition, 1, price=101, identifier="add-second")
        assert not m.consume(first)
        restored = manager(storage).restore()[0].state
        assert restored.add_count == 1
        assert add_quantity(restored, {"max_adds": 1, "add_fraction": "0.5"}, D(102), "BUY") is None


def test_public_unknown_reason_does_not_expose_internal_trade_identifier(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage)
        action = submit(m, plan("internal:NGV6:id"), 1)
        fill(m, action, 1)
        with storage.transaction() as connection:
            connection.execute("UPDATE protection SET confirmed_stop=NULL")
        details = m.portfolio_diagnostics()
        assert details["risk_state"] == "unknown"
        assert "NGV6" not in details["unknown_reason"] and "internal" not in details["unknown_reason"]


def test_partial_exit_reduces_current_risk_but_keeps_d0_and_peak_measure(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, balance=100000)
        p = plan(stop="99.5", costs=CostSnapshot(D(0), D(0)))
        action = OpenTrade("entry", "first", 0, "entry", 3, "limit", D(100))
        m.submit_plan(p, action, price_step=D("0.01"), step_cost=D(2))
        fill(m, action, 3)
        assert m._budget_state(storage.connection).open_risk == D(300)
        state = storage.load_trade("first").state
        partial = ReduceTrade("partial", "first", state.state_revision, "profile-partial", 2)
        m.submit_action(partial)
        fill(m, partial, 2, price="99.9", identifier="partial-fill")
        state = storage.load_trade("first").state
        assert m._budget_state(storage.connection).open_risk == D(100)
        assert state.initial_stop_distance == D("0.5") and state.max_quantity == 3
        assert state.realized_pnl == D(-40)
        row = CsvExporter(storage.connection, tmp_path / "events.csv", tmp_path / "summary.csv")._snapshot()[2][0]
        assert row["initial_risk"] == "300.00" and row["result_r"] == "-0.13"


def test_zero_policy_rejects_a_zero_incremental_risk_add_without_selling_protected_position(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, balance=100000)
        action = submit(m, plan(costs=CostSnapshot(D(0), D(0))), 4)
        fill(m, action, 4)
        with storage.transaction() as connection:
            connection.execute("UPDATE protection SET confirmed_stop='105'")
        restarted = manager(storage, portfolio_pct=0)
        state = storage.load_trade("first").state
        with pytest.raises(ReservationRejected, match="risk-budget"):
            restarted.submit_action(AddToTrade("add", "first", state.state_revision, "signal", 1, "limit", D(106)))
        assert storage.load_trade("first").state.quantity == 4
        assert storage.connection.execute("SELECT action_type FROM orders").fetchall() == [("OPEN",)]


@pytest.mark.parametrize("costs,reward,expected", [(100, 100, "cost-exceeds-reward"), (50, 90, "risk-below-floor")])
def test_cost_then_price_risk_floor_precede_ratio_filters(tmp_path, costs, reward, expected):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, min_trade_risk_pct=D(1))
        economics = PlanEconomics(1, D(80), D(reward), D(costs), (D(reward)-D(costs))/D(80), slippage_amount=D(40))
        result = m._entry_economics_refusal(plan(costs=CostSnapshot(D(0), D(0))), economics, 1, D(80+costs), D(100000))
        assert result.rejections[0].code == expected
        assert storage.connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_disabled_ratios_do_not_disable_cost_exceeds_reward(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, min_risk_cost_ratio=0, max_slippage_r=0, min_net_payoff=0)
        economics = PlanEconomics(1, D(80), D(1000), D(100), D("11.25"), slippage_amount=D(80))
        assert m._entry_economics_refusal(plan(), economics, 1, D(180), D(100000)) is None
        economics = replace(economics, reward_amount=D(100), payoff_ratio=D(0))
        assert m._entry_economics_refusal(plan(), economics, 1, D(180), D(100000)).rejections[0].code == "cost-exceeds-reward"


def test_partial_entry_reallocates_zero_targets_without_fake_completion(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        m = manager(storage, balance=100000)
        p = replace(plan(costs=CostSnapshot(D(0), D(0))),
                    targets=(TargetPlan("tp-1", D(104), D("0.5")), TargetPlan("tp-2", D(108), D("0.5"))),
                    profile=ProfileSnapshot("levels_rr", "1", {}))
        action = submit(m, p, 2)
        fill(m, action, 1, status=ExecutionStatus.PARTIAL, identifier="first-part")
        assert storage.connection.execute("SELECT planned_quantity,status FROM targets ORDER BY target_index").fetchall() == [(0, "PENDING"), (1, "PENDING")]
        assert storage.load_trade("first").state.completed_target_ids == frozenset()
        fill(m, action, 1, identifier="second-part")
        assert storage.connection.execute("SELECT planned_quantity FROM targets ORDER BY target_index").fetchall() == [(1,), (1,)]
