"""Границы, которые change фиксирует требованием, а не соглашением.

Шина обслуживает доставку уведомлений. Решение-трейс, обработка события
исполнения, post-fill проверки и проекции журнала остаются прямыми вызовами
владельца состояния: доставка не гарантирует ни порядок, ни атомарность.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import run
from src.bot import TradingBot
from src.broker.events import BrokerEvent
from src.broker.port import ExecutionEvent, ExecutionStatus
from src.events.bus import EventBus
from src.events.types import EventType
from src.market_context.models import (
    MarketContext,
    SRLevel,
    SRType,
    TrendDirection,
    TrendResult,
)
from src.notifier.templates import render
from src.portfolio.models import ContractMeta
from src.portfolio.risk import RiskLimits
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import Storage
from src.trade_management.actions import MoveStop
from src.trade_management.manager import TradeManager

UTC = timezone.utc
BAR0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
NG_META = ContractMeta(
    ticker="NGV6", price_step=1.0, step_cost=100.0, go_buy=5000.0, go_sell=5000.0
)
PROFILES = {"levels_rr": {"buffer_ticks": 1, "target_R": (1, 2), "shares": (0.5, 0.5)}}
LIMITS = RiskLimits(
    per_trade=Decimal("2"),
    per_instrument=Decimal("6"),
    per_group={},
    portfolio=Decimal("10"),
)
REJECTING_LIMITS = RiskLimits(
    per_trade=Decimal("0.0001"),
    per_instrument=Decimal("0.0001"),
    per_group={},
    portfolio=Decimal("0.0001"),
)


class Recorder:
    """Подписчик шины, который только смотрит и ничего не меняет."""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.supported_types = frozenset(EventType)

    def handle(self, event) -> None:
        self.events.append(event)

    def texts(self) -> list[str | None]:
        return [render(event) for event in self.events]


class _FillBroker:
    def __init__(self) -> None:
        self.actions: list[object] = []
        self._quantity: dict[str, int] = {}

    def register_trade(self, plan) -> None:
        return None

    def contract_for(self, ticker: str) -> ContractMeta | None:
        return NG_META

    def submit(self, action, now):
        self.actions.append(action)
        quantity = self._quantity.get(action.trade_id, 0) + getattr(action, "quantity", 0)
        self._quantity[action.trade_id] = quantity
        return ExecutionEvent(
            f"{action.command_id}:fill",
            action.command_id,
            action.command_id,
            action.trade_id,
            ExecutionStatus.FILL,
            quantity,
            Decimal("100"),
            Decimal("0"),
            now,
            action.reason,
        )


def _assignment() -> SimpleNamespace:
    return SimpleNamespace(
        id="assignment-1",
        strategy="macd_rsi_stoch",
        management="levels_rr",
        filter_profile="basic_levels",
        priority=0,
        timeframe="15m",
    )


def _context() -> MarketContext:
    return MarketContext(
        trend=TrendResult(direction=TrendDirection.UP, strength=0.6),
        sr_levels=[SRLevel(97.0, SRType.SUPPORT, 2, "s1")],
        current_price=100.0,
    )


def _frame(closes) -> pd.DataFrame:
    index = pd.date_range("2026-01-01 09:00", periods=len(closes), freq="min", tz="UTC")
    return pd.DataFrame(
        {
            "datetime": index,
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
        }
    )


def _manager(storage, broker, limits: RiskLimits = LIMITS) -> TradeManager:
    return TradeManager(
        storage,
        broker,
        initial_balance=Decimal("100000"),
        profiles_config=PROFILES,
        risk_limits=limits,
        max_qty=4,
    )


def _admit(manager: TradeManager):
    return manager.actions_for_signal(
        _assignment(),
        Decision(
            signal_type=SignalType.BUY,
            price=100.0,
            bar_time=BAR0,
            event_id="signal-1",
            available_at=BAR0,
            timeframe="15m",
        ),
        SimpleNamespace(ticker="NGV6", short_name="NG"),
        _frame([100.0] * 25),
        _context(),
        timeframe="15m",
    )


def _traces(storage) -> int:
    return storage.connection.execute("SELECT COUNT(*) FROM calculations").fetchone()[0]


def _recorded_quantity(storage, trade_id: str) -> int:
    return storage.connection.execute(
        "SELECT quantity FROM positions WHERE trade_id = ?", (trade_id,)
    ).fetchone()[0]


def _closed_event(trade_id: str, command_id: str, quantity: int) -> ExecutionEvent:
    return ExecutionEvent(
        "e-close", None, command_id, trade_id, ExecutionStatus.FILL,
        quantity, Decimal("100"), Decimal("0"), BAR0, "entry",
    )


class TestTraceSharesTheDecisionTransaction:
    def test_rejection_by_risk_limit_still_leaves_a_calculation_record(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3", audit_path=tmp_path / "trace.log") as storage:
            admission = _admit(_manager(storage, _FillBroker(), REJECTING_LIMITS))

            assert admission.plan is None
            assert admission.rejections
            assert _traces(storage) >= 1, "отказ по лимиту обязан оставить след расчёта"

    def test_trace_rolls_back_with_the_decision_it_belongs_to(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3", audit_path=tmp_path / "trace.log") as storage:
            admission = _admit(_manager(storage, _FillBroker()))
            assert admission.plan is not None
            trade_id = admission.plan.trade_id
            before = _traces(storage)

            with pytest.raises(RuntimeError):
                with storage.transaction() as failed:
                    failed.execute(
                        "UPDATE trades SET state_revision = state_revision + 1 WHERE trade_id = ?",
                        (trade_id,),
                    )
                    raise RuntimeError("решение не сохранено")

            assert _traces(storage) == before
            assert storage.connection.execute(
                "SELECT state_revision FROM trades WHERE trade_id = ?", (trade_id,)
            ).fetchone()[0] == 0

    def test_calculation_never_reaches_the_bus(self, tmp_path):
        recorder = Recorder()
        bus = EventBus()
        bus.subscribe(recorder)
        with Storage(tmp_path / "trades.sqlite3", audit_path=tmp_path / "trace.log") as storage:
            _admit(_manager(storage, _FillBroker()))

        assert recorder.events == [], "решение-трейс не публикуется в шину"


class TestPostFillStaysDirect:
    def test_post_fill_runs_after_the_reducer_recorded_the_fill(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3", audit_path=tmp_path / "trace.log") as storage:
            manager = _manager(storage, _FillBroker())
            admission = _admit(manager)
            trade_id = admission.plan.trade_id
            action = admission.actions[0]
            assert _recorded_quantity(storage, trade_id) == 0

            seen: list[tuple[str, int]] = []
            manager._post_fill_check = lambda event: seen.append(
                (event.trade_id, _recorded_quantity(storage, event.trade_id))
            ) or []
            manager.consume(_closed_event(trade_id, action.command_id, action.quantity))

            assert seen == [(trade_id, action.quantity)], (
                "post-fill проверка должна видеть уже записанное редьюсером состояние"
            )

    def test_post_fill_does_not_repeat_for_an_already_applied_event(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3", audit_path=tmp_path / "trace.log") as storage:
            manager = _manager(storage, _FillBroker())
            admission = _admit(manager)
            trade_id = admission.plan.trade_id
            action = admission.actions[0]
            seen: list[str] = []
            manager._post_fill_check = lambda event: seen.append(event.execution_id) or []
            event = _closed_event(trade_id, action.command_id, action.quantity)

            assert manager.consume(event) is True
            assert manager.consume(event) is False
            assert seen == [event.execution_id]

    def test_management_action_is_queued_with_the_current_revision_only(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3", audit_path=tmp_path / "trace.log") as storage:
            manager = _manager(storage, _FillBroker())
            admission = _admit(manager)
            trade_id = admission.plan.trade_id
            manager.dispatch(BAR0)
            revision = storage.connection.execute(
                "SELECT state_revision FROM trades WHERE trade_id = ?", (trade_id,)
            ).fetchone()[0]

            with pytest.raises(ValueError, match="stale-state-revision"):
                manager.submit_action(
                    MoveStop("command-stale", trade_id, revision - 1, "move-stop", Decimal("99"))
                )

            assert manager.submit_action(
                MoveStop("command-fresh", trade_id, revision, "move-stop", Decimal("99"))
            )
            assert storage.connection.execute(
                "SELECT pending_stop FROM protection WHERE trade_id = ?", (trade_id,)
            ).fetchone()[0] == "99"
            assert storage.connection.execute(
                "SELECT command_id FROM outbox WHERE trade_id = ? AND command_id = ?",
                (trade_id, "command-fresh"),
            ).fetchone()[0] == "command-fresh"


class TestNoStateChangingSubscribers:
    def test_bot_takes_neither_an_execution_port_nor_a_risk_manager(self):
        parameters = inspect.signature(TradingBot.__init__).parameters

        assert "execution" not in parameters
        assert "risk_manager" not in parameters

    def test_execution_outcome_is_applied_by_a_direct_call(self):
        source = (ROOT / "run.py").read_text(encoding="utf-8")

        assert "trade_manager.consume(event)" in source

    def test_translation_bridge_and_execution_port_are_gone(self):
        assert not (ROOT / "src" / "events" / "notify_bridge.py").exists()
        assert not (ROOT / "src" / "execution").exists()

    def test_bot_publishes_without_a_translation_helper(self):
        source = (ROOT / "src" / "bot" / "trading_bot.py").read_text(encoding="utf-8")

        assert "notify_" not in source
        assert "self._bus.publish" in source

    def test_no_subscriber_outside_notifier_is_subscribed_to_the_bus(self):
        source = (ROOT / "run.py").read_text(encoding="utf-8")
        subscribed = [
            line.strip()
            for line in source.splitlines()
            if ".subscribe" in line and "notifier" not in line and "channel" not in line
        ]

        assert subscribed == [], f"шина обслуживает не только уведомления: {subscribed}"


class TestOneFactOneEvent:
    def test_one_close_produces_one_trade_closed_event(self, tmp_path, monkeypatch):
        from src.broker import create_journal_broker
        from src.portfolio import Signal

        monkeypatch.setattr("src.config_loader.app_dir", lambda: tmp_path)
        broker = create_journal_broker(
            "j.csv", 100000, 2.0, ["14:05"], None, {"NG": "NG-10.26"}
        )
        broker.place_order(
            Signal(
                position_id="NG-123", ticker="NG", side="BUY", entry_price=100.0,
                stop_price=98.0, stop_distance_pct=2.0, risk_pct=2.0, risk_rub=2000.0,
                take_profit=None, timeframe="1h", source="ma", qty=10,
            ),
            NG_META,
            BAR0,
        )
        broker.track_bar(
            BAR0 + timedelta(minutes=1), {"NG": (99.0, 101.0, 100.0)}, {"NG": NG_META}
        )
        broker.drain_events()
        position = next(iter(broker.manager.positions.values()))

        broker.close_position(position, 104.0, BAR0 + timedelta(minutes=2), "protective")

        assert [event.type for event in broker.drain_events()] == [EventType.TRADE_CLOSED]


class TestTradeIdStaysInternal:
    def _closed(self) -> object:
        return run._broker_event(
            BrokerEvent(
                type=EventType.TRADE_CLOSED,
                ts=BAR0,
                trade_id="trade-1",
                instrument="NG-9.26",
                payload={
                    "quantity": 2,
                    "price": Decimal("110"),
                    "pnl": Decimal("200"),
                    "reason": "protective",
                },
            )
        )

    def test_trade_id_is_carried_for_correlation_and_hidden_from_text(self):
        recorder = Recorder()
        bus = EventBus()
        bus.subscribe(recorder)
        bus.publish(self._closed())

        event = recorder.events[0]
        assert event.get("trade_id") == "trade-1"
        assert "trade-1" not in (recorder.texts()[0] or "")
        assert "ВЫХОД · исполнено 2 по 110" in recorder.texts()[0]

    def test_clearing_event_carries_no_trade_and_no_contract(self):
        recorder = Recorder()
        bus = EventBus()
        bus.subscribe(recorder)
        bus.publish(
            run._broker_event(
                BrokerEvent(
                    type=EventType.CLEARING_DONE,
                    ts=BAR0,
                    payload={"balance": Decimal("100000"), "positions": 2},
                )
            )
        )

        event = recorder.events[0]
        assert event.instrument == ""
        assert "trade_id" not in event.payload
        text = recorder.texts()[0]
        assert "NGV6" not in text
        assert "100 000 ₽" in text
        assert "2" in text


class TestContractNameRule:
    def _bot(self, short_name: str | None, ticker: str = "NGV6", inst_type: str = "futures") -> tuple[TradingBot, Recorder]:
        recorder = Recorder()
        bus = EventBus()
        bus.subscribe(recorder)
        strategy = SimpleNamespace(
            NAME="macd_rsi_stoch",
            decide=lambda *args, **kwargs: Decision(SignalType.BUY, 100.5),
        )
        bot = TradingBot(
            instruments=[("NG (Природный газ) — NG-9.26", ticker, inst_type, short_name)],
            bus=bus,
            strategy_map={"macd_rsi_stoch": object()},
            data_cache=None,
            timeline=None,
            strategy_factory=lambda name, config: strategy,
        )
        return bot, recorder

    def test_missing_short_name_yields_placeholder_instead_of_a_raw_ticker(self):
        bot, recorder = self._bot(None)

        bot._publish_decision(
            Decision(SignalType.BUY, 100.5), bot._instruments[0], timeframe="1h"
        )

        event = recorder.events[0]
        assert event.instrument == "контракт не указан"
        text = recorder.texts()[0]
        assert "контракт не указан" in text

    def test_share_is_named_by_its_ticker(self):
        bot, recorder = self._bot(None, ticker="SBER", inst_type="share")

        bot._publish_decision(
            Decision(SignalType.BUY, 100.5), bot._instruments[0], timeframe="15m"
        )

        event = recorder.events[0]
        assert event.instrument == "SBER"
        text = recorder.texts()[0]
        assert "SBER" in text
        assert "контракт не указан" not in text
        assert "NGV6" not in text
        assert "NG (Природный" not in text

    def test_known_short_name_is_used_verbatim(self):
        bot, recorder = self._bot("NG-9.26")

        bot._publish_decision(
            Decision(SignalType.BUY, 100.5), bot._instruments[0], timeframe="1h"
        )

        assert recorder.events[0].instrument == "NG-9.26"
        assert "NG-9.26" in recorder.texts()[0]
