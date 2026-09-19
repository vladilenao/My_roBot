from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from src.broker import create_addressable_journal_broker
from src.broker.port import ExecutionStatus
from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.portfolio.models import ContractMeta
from src.portfolio.risk import RiskLimits
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade, OpenTrade, TradeAction
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BAR0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
BAR1 = datetime(2026, 1, 1, 10, 1, tzinfo=timezone.utc)

NG_META = ContractMeta(ticker="NGV6", price_step=1.0, step_cost=100.0, go_buy=5000.0, go_sell=5000.0)

PROFILES = {
    "levels_rr": {"buffer_ticks": 1, "target_R": (1, 2), "shares": (0.5, 0.5)},
    "ma_cloud": {"buffer_ticks": 1, "ma_fast_period": 10, "ma_slow_period": 40},
}
LIMITS = RiskLimits(
    per_trade=Decimal("2"), per_instrument=Decimal("6"),
    per_group={}, portfolio=Decimal("10"),
)

INSTRUMENT = SimpleNamespace(ticker="NGV6", short_name="NG")


def _frame(closes, start="2026-01-01 09:00"):
    index = pd.date_range(start, periods=len(closes), freq="min", tz="UTC")
    return pd.DataFrame(
        {"datetime": index, "open": closes, "high": [c + 1.0 for c in closes],
         "low": [c - 1.0 for c in closes], "close": closes}
    )


def _context(*, price=100.0, levels=()):
    return MarketContext(
        trend=TrendResult(direction=TrendDirection.UP, strength=0.6),
        sr_levels=list(levels),
        current_price=price,
    )


class _FillBroker:
    def __init__(self, meta: ContractMeta = NG_META) -> None:
        self.actions = []
        self._meta = meta
        self._quantity: dict[str, int] = {}

    def register_trade(self, plan: TradePlan) -> None:
        return None

    def contract_for(self, ticker: str) -> ContractMeta | None:
        return self._meta

    def submit(self, action, now):
        self.actions.append(action)
        from src.broker.port import ExecutionEvent
        if isinstance(action, OpenTrade):
            quantity = action.quantity
            self._quantity[action.trade_id] = self._quantity.get(action.trade_id, 0) + quantity
        else:
            quantity = self._quantity.get(action.trade_id, 0)
            self._quantity[action.trade_id] = max(0, self._quantity.get(action.trade_id, 0) - quantity)
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, quantity, Decimal("100"), Decimal("0"), now, action.reason,
        )


def _assign_context(decision):
    return {
        "context": _context(price=decision.price,
                             levels=(SRLevel(97.0, SRType.SUPPORT, 2, "s1"),)),
    }


def test_actions_for_signal_plans_sizes_and_fills_entry(tmp_path):
    assignment = SimpleNamespace(id="assignment-1", strategy="macd_rsi_stoch",
                                 management="levels_rr", filter_profile="basic_levels",
                                 priority=0, timeframe="15m")
    decision = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0,
                        event_id="signal-1", available_at=BAR0, timeframe="15m")
    broker = _FillBroker()
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        actions = manager.actions_for_signal(
            assignment, decision, INSTRUMENT, _frame([100.0] * 25), _context(price=100.0,
            levels=(SRLevel(97.0, SRType.SUPPORT, 2, "s1"),)), timeframe="15m",
        )

        assert len(actions) == 1
        opening = actions[0]
        assert isinstance(opening, OpenTrade)
        assert opening.quantity == 4
        assert storage.connection.execute(
            "SELECT phase FROM trades WHERE trade_id = ?", (opening.trade_id,)
        ).fetchone()[0] == "ENTRY_PENDING"

        manager.dispatch(BAR0)
        phase = storage.connection.execute(
            "SELECT phase FROM trades WHERE trade_id = ?", (opening.trade_id,)
        ).fetchone()[0]
        assert phase == "OPEN"


def test_actions_for_signal_closes_owned_trade_on_opposite_signal(tmp_path):
    assignment = SimpleNamespace(id="assignment-1", strategy="macd_rsi_stoch",
                                 management="levels_rr", filter_profile="basic_levels",
                                 priority=0, timeframe="15m")
    entry = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0,
                     event_id="signal-1", available_at=BAR0, timeframe="15m")
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        actions = manager.actions_for_signal(
            assignment, entry, INSTRUMENT, _frame([100.0] * 25),
            _context(levels=(SRLevel(97.0, SRType.SUPPORT, 2, "s1"),)), timeframe="15m",
        )
        manager.dispatch(BAR0)

        oppose = Decision(signal_type=SignalType.SELL, price=98.0, bar_time=BAR1,
                          event_id="signal-2", available_at=BAR1, timeframe="15m")
        actions = manager.actions_for_signal(
            assignment, oppose, INSTRUMENT, _frame([98.0] * 25), _context(price=98.0,
            levels=(SRLevel(99.0, SRType.RESISTANCE, 2, "r1"),)), timeframe="15m",
        )
        assert isinstance(actions[0], CloseTrade)

        manager.dispatch(BAR1)
        phase = storage.connection.execute(
            "SELECT phase FROM trades WHERE trade_id = ?", (actions[0].trade_id,)
        ).fetchone()[0]
        assert phase == "CLOSED"


def test_manage_queues_signal_exit_from_ma_cloud(tmp_path):
    plan = TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("92"),
        (TargetPlan("tp-1", Decimal("103"), Decimal("1")),),
        ProfileSnapshot("ma_cloud", "1", {"buffer_ticks": 1}), NOW,
    )
    broker = _FillBroker()
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        assert manager.submit_plan(plan, OpenTrade("open-1", "trade-1", 0, "entry", 2))
        manager.dispatch(BAR0)

        declining = [120.0 - i * (40.0 / 44.0) for i in range(45)]
        actions = manager.manage(INSTRUMENT, [], _frame(declining), None, timeframe="1m")

        assert any(isinstance(action, CloseTrade) for action in actions)
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'PENDING'"
        ).fetchone()[0] == 1


def test_manage_does_not_propose_adds_without_entry_price(tmp_path):
    plan = TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("104"), Decimal("0.5")), TargetPlan("tp-2", Decimal("108"), Decimal("0.5"))),
        ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1, "max_adds": 1, "add_fraction": "0.5"}), NOW,
    )
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        assert manager.submit_plan(plan, OpenTrade("open-1", "trade-1", 0, "entry", 2))
        manager.dispatch(BAR0)
        actions = manager.manage(INSTRUMENT, [], _frame([102.0] * 25), None, timeframe="1m")
        assert not any(isinstance(action, TradeAction) for action in actions)


def test_next_bar_scheduled_entry_fills_and_reaches_journal(tmp_path):
    broker = create_addressable_journal_broker(1_000_000, [], {})
    broker.set_contracts({"NGV6": NG_META})
    plan = TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("103"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
    )
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        assert manager.submit_plan(plan, OpenTrade("open-1", "trade-1", 0, "entry", 2))
        events = manager.dispatch(BAR0)
        assert events[0].status is ExecutionStatus.ACK

        broker.track_bar(BAR1, {"NGV6": (100.0, 99.5, 101.0, 100.5)}, {"NGV6": NG_META})
        for event in broker.drain_addressed_events():
            manager.consume(event)

        phase, quantity = storage.connection.execute(
            "SELECT t.phase, p.quantity FROM trades t JOIN positions p ON p.trade_id = t.trade_id WHERE t.trade_id = ?",
            (plan.trade_id,),
        ).fetchone()
        assert phase == "OPEN"
        assert quantity == 2


def test_naive_bar_time_activates_scheduled_entry_without_type_error(tmp_path):
    broker = create_addressable_journal_broker(1_000_000, [], {})
    broker.set_contracts({"NGV6": NG_META})
    plan = TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("103"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
    )
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, broker, initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        assert manager.submit_plan(plan, OpenTrade("open-1", "trade-1", 0, "entry", 2))
        events = manager.dispatch(BAR0)
        assert events[0].status is ExecutionStatus.ACK

        broker.track_bar(
            datetime(2026, 1, 1, 10, 2),
            {"NGV6": (100.0, 99.5, 101.0, 100.5)},
            {"NGV6": NG_META},
        )
        fill = broker.drain_addressed_events()
        assert fill
        for event in fill:
            manager.consume(event)

        phase, quantity = storage.connection.execute(
            "SELECT t.phase, p.quantity FROM trades t JOIN positions p ON p.trade_id = t.trade_id WHERE t.trade_id = ?",
            (plan.trade_id,),
        ).fetchone()
        assert phase == "OPEN"
        assert quantity == 2


class _BrokerWithoutMetadata:
    def contract_for(self, ticker: str):
        return None

    def submit(self, action, now):
        raise AssertionError("broker must not be called without contract metadata")


def test_actions_for_signal_rejects_without_contract_metadata(tmp_path):
    assignment = SimpleNamespace(id="assignment-1", strategy="macd_rsi_stoch",
                                 management="levels_rr", filter_profile="basic_levels",
                                 priority=0, timeframe="15m")
    decision = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0,
                        event_id="signal-1", available_at=BAR0, timeframe="15m")
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, _BrokerWithoutMetadata(), initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        admission = manager.actions_for_signal(
            assignment, decision, INSTRUMENT, _frame([100.0] * 25), _context(price=100.0), timeframe="15m",
        )
        assert len(admission) == 0
        assert len(admission.rejections) == 1
        assert admission.rejections[0].code == "no-contract-metadata"
        assert admission.rejections[0].message


def test_actions_for_signal_rejects_duplicate_signal(tmp_path):
    assignment = SimpleNamespace(id="assignment-1", strategy="macd_rsi_stoch",
                                 management="levels_rr", filter_profile="basic_levels",
                                 priority=0, timeframe="15m")
    decision = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0,
                        event_id="signal-1", available_at=BAR0, timeframe="15m")
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        first = manager.actions_for_signal(
            assignment, decision, INSTRUMENT, _frame([100.0] * 25),
            _context(levels=(SRLevel(97.0, SRType.SUPPORT, 2, "s1"),)), timeframe="15m",
        )
        assert len(first) == 1
        with storage.transaction() as connection:
            connection.execute(
                "UPDATE trades SET phase = 'CLOSED' WHERE trade_id = ?", (first[0].trade_id,)
            )
        admission = manager.actions_for_signal(
            assignment, decision, INSTRUMENT, _frame([100.0] * 25),
            _context(levels=(SRLevel(97.0, SRType.SUPPORT, 2, "s1"),)), timeframe="15m",
        )
        assert len(admission) == 0
        assert [reason.code for reason in admission.rejections] == ["duplicate-signal"]
        assert admission.rejections[0].message == "дублирующий сигнал, сделка не взята в работу"


def test_actions_for_signal_distinct_profiles_get_distinct_trade_ids(tmp_path):
    assignment_a = SimpleNamespace(id="assignment-a", strategy="macd_rsi_stoch",
                                   management="levels_rr", filter_profile="basic_levels",
                                   priority=0, timeframe="15m")
    assignment_b = SimpleNamespace(id="assignment-b", strategy="macd_rsi_stoch",
                                   management="ma_cloud", filter_profile="raw",
                                   priority=0, timeframe="15m")
    bar_time = "2026-01-01T10:00:00+00:00"
    decision_a = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0,
                          event_id=f"assignment-a:NGV6:15m:{bar_time}:BUY",
                          available_at=BAR0, timeframe="15m")
    decision_b = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0,
                          event_id=f"assignment-b:NGV6:15m:{bar_time}:BUY",
                          available_at=BAR0, timeframe="15m")
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        first = manager.actions_for_signal(
            assignment_a, decision_a, INSTRUMENT, _frame([100.0] * 25),
            _context(levels=(SRLevel(97.0, SRType.SUPPORT, 2, "s1"),)), timeframe="15m",
        )
        second = manager.actions_for_signal(
            assignment_b, decision_b, INSTRUMENT, _frame([100.0] * 45),
            _context(price=100.0), timeframe="15m",
        )
        assert len(first) == 1
        assert len(second) == 1
        assert first[0].trade_id == decision_a.event_id
        assert second[0].trade_id == decision_b.event_id
        assert first[0].trade_id != second[0].trade_id
        rows = storage.connection.execute(
            "SELECT trade_id, phase FROM trades ORDER BY trade_id"
        ).fetchall()
        assert {row[0] for row in rows} == {first[0].trade_id, second[0].trade_id}
        assert {row[1] for row in rows} == {"ENTRY_PENDING"}


def test_submit_plan_returns_false_for_existing_trade_id(tmp_path):
    plan = TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("103"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
    )
    retry = TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-2", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("103"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer_ticks": 1}), NOW,
    )
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=LIMITS, max_qty=4)
        assert manager.submit_plan(plan, OpenTrade("open-1", "trade-1", 0, "entry", 2))
        assert not manager.submit_plan(retry, OpenTrade("open-2", "trade-1", 0, "entry", 2))
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM trades WHERE trade_id = ?", ("trade-1",)
        ).fetchone()[0] == 1


def test_actions_for_signal_rejects_zero_quantity(tmp_path):
    assignment = SimpleNamespace(id="assignment-1", strategy="macd_rsi_stoch",
                                 management="levels_rr", filter_profile="basic_levels",
                                 priority=0, timeframe="15m")
    decision = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0,
                        event_id="signal-1", available_at=BAR0, timeframe="15m")
    zero_limits = RiskLimits(
        per_trade=Decimal("0"), per_instrument=Decimal("0"),
        per_group={}, portfolio=Decimal("0"),
    )
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"),
                               profiles_config=PROFILES, risk_limits=zero_limits, max_qty=4)
        admission = manager.actions_for_signal(
            assignment, decision, INSTRUMENT, _frame([100.0] * 25),
            _context(levels=(SRLevel(97.0, SRType.SUPPORT, 2, "s1"),)), timeframe="15m",
        )
        assert len(admission) == 0
        assert [reason.code for reason in admission.rejections] == ["zero-quantity"]