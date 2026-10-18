"""Календарные переходы с настоящими broker/reducer/SQLite и известным PnL."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from contextlib import contextmanager
import json

import pandas as pd
import pytest

from src.broker.journal_broker import JournalBroker
from src.broker.port import ExecutionStatus, ExecutionEvent, FeeSource
from src.data.cache import MarketDataCache
from src.history.contract_lifecycle import RolloverLifecycle, ContractLifecycleError
from src.history.report import RunMetrics, collect_result, collect_rollover, write_report
from src.history.selection import HistorySelection
from src.instruments.futures import FuturesCard
from src.instruments.model import instrument_key
from src.instruments.model import Instrument
from src.portfolio import ContractMeta, PositionManager
from src.scheduler.clock import HistoricalClock, as_aware
from src.scheduler.timing import MultiTimeframeScheduler
from src.trade_journal.storage import Storage
from src.trade_management.actions import OpenTrade, AddToTrade, EntryOrderType, CancelEntry, CloseTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import TradePlan, ProfileSnapshot, CostSnapshot, CURRENT_ALGORITHM
from src.bot import TradingBot
from src.bot.run_session import HistoricalRunSession
from src.events.bus import EventBus
from src.market_context.models import MarketContext, SRLevel, SRType, TrendResult, TrendDirection
from src.strategies.contracts import Assignment, Decision, SignalType
from src.trade_management.intraday_settings import IntradaySettings

D = Decimal
START = datetime(2026, 1, 1, 10)
END = START+timedelta(minutes=16)


def selected():
    cards = []
    for i in (1, 2, 3):
        cards.append(FuturesCard.from_row({
            "instrument_id": f"uid-{i}", "ticker": f"NG-{i}.26", "name": f"NG-{i}.26 Газ",
            "first_trade_date": "2025-12-01T07:00:00Z",
            "expiration_date": (START+timedelta(days=2, minutes=i*5)).isoformat()+"Z",
            "min_price_increment": 1, "min_price_increment_amount": 10,
            "initial_margin_on_buy": 100, "initial_margin_on_sell": 100,
        }))
    # Последний выпуск живёт до конца диапазона: его cutoff позже end.
    cards[-1] = replace(cards[-1], expiration_date=START+timedelta(days=3))
    return HistorySelection(families=("NG",), catalog=tuple(cards)).planned(START, END, {}, 2)


def base_price(uid):
    if uid == "share":
        return 10
    return (80 if uid.startswith("br-") else 100)+(int(uid.split("-")[1])-1)*50


def source(**kw):
    uid = kw["instrument_id"]
    i = 100 if uid == "share" else int(uid.split("-")[1])
    from_ = kw["start_date"] or START-timedelta(minutes=10)
    to = kw.get("end_date") or kw["clock"].now()
    stamps = pd.date_range(from_, to, freq="1min", inclusive="left")
    base = base_price(uid)
    cutoff = START+timedelta(minutes=i*5+(2 if uid.startswith("br-") else 0))
    prices = [base+(3 if stamp >= cutoff else 0) for stamp in stamps]
    frame = pd.DataFrame({"datetime": stamps, "open": prices, "close": prices,
                          "low": [p-1 for p in prices], "high": [p+1 for p in prices], "volume": 100})
    return frame, kw["instrument_id"]


def plan(trade, card, when):
    price = D(base_price(card.uid))
    return TradePlan(trade, "binding", card.uid, "BUY", f"signal-{trade}", price, price-4,
        (), ProfileSnapshot("levels_rr", "1", {}), as_aware(when), timeframe="1m",
        algorithm_version=CURRENT_ALGORITHM, cost_snapshot=CostSnapshot(D(1), D(0)),
        entry_order_type=EntryOrderType.LIMIT)


def setup(tmp_path, pause=0, selection=None):
    observed_pauses = []
    clock = HistoricalClock(START, END, timedelta(minutes=1), pause, sleeper=observed_pauses.append)
    timeline = MultiTimeframeScheduler(["1m"], clock=clock)
    cache = MarketDataCache(source, timeline, clock=clock)
    storage = Storage(tmp_path / "trades.sqlite3", initial_deposit="100000", clock=clock)
    broker = JournalBroker(None, PositionManager(initial_deposit=100000, max_risk_pct=2), [], clock=clock)
    selection = selection or selected()
    metadata = {card.uid: ContractMeta(card.uid, 1, 10, 100, 100) for card in selection.catalog}
    for item in selection.fixed:
        metadata[instrument_key(item)] = ContractMeta(instrument_key(item), 1, 1, 0, 0)
    broker.set_contracts(metadata)
    names = {instrument_key(item): item.short_name for item in selection.instruments}
    broker.set_names(names)
    storage.set_names(names)
    manager = TradeManager(storage, broker, initial_balance=D(100000), clock=clock,
        commission=1, slippage=0, contract_expiry_block_days=2)
    manager.initialize_account()
    lifecycle = RolloverLifecycle(selection, storage, manager, cache, lambda family: {"1m": 2}, END)
    manager.contract_lifecycle = lifecycle
    manager.configure_visual_context(selection.instruments)
    broker.increase_allowed = lifecycle.execution_increase_allowed
    return clock, cache, storage, broker, manager, lifecycle, metadata, observed_pauses


def step(clock, cache, broker, manager, lifecycle, metadata):
    clock.advance()
    now = clock.now()
    lifecycle.before_tick(now)
    for instrument in lifecycle.market_instruments():
        cache.ensure_loaded(instrument, "1m")
    bar_open = now-timedelta(minutes=1)
    prices = {}
    for instrument in lifecycle.execution_instruments():
        frame = cache.frame_for(instrument, "1m")
        row = frame.loc[frame["datetime"] == bar_open]
        if not row.empty:
            row = row.iloc[0]
            prices[instrument_key(instrument)] = (row["open"], row["low"], row["high"], row["close"])
    if prices:
        broker.track_bar(as_aware(bar_open), prices, metadata)
        for event in broker.drain_addressed_events():
            manager.consume(event)
        manager.observe_bars(prices, {ticker: as_aware(bar_open) for ticker in prices})
        manager.mark_to_market({ticker: bar[3] for ticker, bar in prices.items()})
    lifecycle.after_execution(now)


@pytest.mark.parametrize("pause", [0, .5])
def test_three_contract_chain_has_real_close_costs_no_basis_pnl(tmp_path, pause):
    clock, cache, storage, broker, manager, lifecycle, metadata, pauses = setup(tmp_path, pause)
    try:
        created = set()
        while clock.now() < END:
            step(clock, cache, broker, manager, lifecycle, metadata)
            state = lifecycle.states["NG"]
            index = state["index"]
            if state["phase"] == "ACTIVE" and index not in created and clock.now() < END:
                card = lifecycle.chains["NG"][index].card
                trade = f"trade-{index}"
                p = plan(trade, card, clock.now())
                action = OpenTrade(f"entry-{index}", trade, 0, "entry", 1, EntryOrderType.LIMIT, p.reference_entry)
                manager.submit_plan(p, action, price_step=D(1), step_cost=D(10))
                manager.dispatch(clock.now())
                created.add(index)
        result = collect_result(storage)
        assert len(created) == 3
        assert result.realized_pnl == 60  # Два закрытия +3 при множителе 10.
        assert result.fees == 5  # Три входа и два выхода по 1.
        assert result.net_pnl == 55
        assert result.balance == 100055
        assert result.equity == 100085  # Открытый последний контракт вырос внутри собственного ряда на 3.
        report = collect_rollover(storage, lifecycle.selection)
        assert len(report["transitions"]) == 2
        assert all(e["status"] == "active" and e["exit_fees"] == "1" for e in report["transitions"])
        assert report["transitions"][0]["old_flat_at"] == "2026-01-01T10:06:00Z"
        assert report["transitions"][1]["old_flat_at"] == "2026-01-01T10:11:00Z"
        assert all(e["activation_at"] == e["old_flat_at"] for e in report["transitions"])
        next_card = lifecycle.chains["NG"][-1].card
        assert lifecycle.signal_allowed(next_card.instrument, START+timedelta(minutes=11), None, clock.now())
        assert not lifecycle.signal_allowed(next_card.instrument, START+timedelta(minutes=10), None, clock.now())
        assert not lifecycle.signal_allowed(next_card.instrument, START+timedelta(minutes=11), START, clock.now())
        if pause:
            assert pauses and set(pauses) == {.5}
        write_report(tmp_path, RunMetrics(start=START, end=END, covered=True, rollover=report), result)
        assert json.loads((tmp_path / "report.json").read_text())["rollover"]["transitions"] == report["transitions"]
        assert "NG-1.26 → NG-2.26" in (tmp_path / "report.txt").read_text()
        restored = RolloverLifecycle(lifecycle.selection, storage, manager, cache, lambda family: {"1m": 2}, END)
        assert restored.states == lifecycle.states
    finally:
        storage.close()


@pytest.mark.parametrize("offset_seconds", [-30, 0, 30])
def test_pending_entry_on_cutoff_cancels_without_fill(tmp_path, offset_seconds):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("waiting", card, clock.now())
        manager.submit_plan(p, OpenTrade("pending", "waiting", 0, "entry", 1, EntryOrderType.LIMIT, D(99)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        # Цена до границы не касалась лимита: после cutoff его нельзя исполнить.
        broker.increase_allowed = lifecycle.execution_increase_allowed
        clock.jump_to(START+timedelta(minutes=7))
        lifecycle.before_tick(clock.now())
        bar_open = START+timedelta(minutes=5, seconds=offset_seconds)
        broker.track_bar(as_aware(bar_open), {card.uid: (99, 98, 100, 99)}, metadata)
        events = broker.drain_addressed_events()
        assert events and all(e.status is ExecutionStatus.CANCEL for e in events)
        for event in events:
            manager.consume(event)
        lifecycle.after_execution(clock.now())
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    finally:
        storage.close()


def test_unsubmitted_entry_and_add_are_removed_without_delivery(tmp_path):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("unsent", card, clock.now())
        manager.submit_plan(p, OpenTrade("unsent-entry", "unsent", 0, "entry", 1, EntryOrderType.LIMIT, D(100)),
                            price_step=D(1), step_cost=D(10))
        clock.jump_to(START+timedelta(minutes=5))
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        assert not manager.contract_has_obligations(card.uid)
        assert storage.load_trade("unsent", include_terminal=True).state.quantity == 0
        assert storage.connection.execute("SELECT status FROM orders WHERE command_id='unsent-entry'").fetchone()[0] == "CANCEL"
    finally:
        storage.close()


def test_partial_entry_pending_add_and_duplicate_close(tmp_path):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("partial", card, clock.now())
        manager.submit_plan(p, OpenTrade("partial-entry", "partial", 0, "entry", 2, EntryOrderType.LIMIT, D(100)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        manager.consume(ExecutionEvent("partial-fill", "partial-entry", "partial-entry", "partial",
            ExecutionStatus.PARTIAL, 1, D(100), D(1), as_aware(clock.now()), "partial-entry", fee_source=FeeSource.CONFIGURED))
        manager.restore()
        state = storage.load_trade("partial").state
        manager.submit_action(AddToTrade("unsent-add", "partial", state.state_revision, "add", 1, EntryOrderType.LIMIT, D(99)))
        clock.jump_to(START+timedelta(minutes=5))
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        lifecycle.after_execution(clock.now())
        assert lifecycle.states["NG"]["phase"] == "DRAINING"
        assert storage.connection.execute("SELECT COUNT(*) FROM orders WHERE action_type='CLOSE'").fetchone()[0] == 1
        assert storage.connection.execute("SELECT status FROM outbox WHERE command_id='unsent-add'").fetchone()[0] == "DISCARDED"
        step(clock, cache, broker, manager, lifecycle, metadata)
        assert storage.load_trade("partial", include_terminal=True).state.quantity == 0
        assert lifecycle.states["NG"]["phase"] == "ACTIVE"
        assert storage.connection.execute("SELECT filled_quantity FROM orders WHERE command_id='partial-entry'").fetchone()[0] == 1
    finally:
        storage.close()


def test_deadline_with_unclosed_position_does_not_activate_next(tmp_path):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("held", card, clock.now())
        manager.submit_plan(p, OpenTrade("held-entry", "held", 0, "entry", 1, EntryOrderType.LIMIT, D(100)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        step(clock, cache, broker, manager, lifecycle, metadata)
        lifecycle.before_tick(START+timedelta(minutes=5))
        lifecycle.after_execution(START+timedelta(minutes=5))
        with pytest.raises(ContractLifecycleError, match="не завершены"):
            lifecycle.after_execution(card.expiration_date)
        assert storage.load_trade("held").state.quantity == 1
        assert lifecycle.states["NG"]["phase"] == "DRAINING"
        assert collect_rollover(storage, lifecycle.selection)["transitions"][0]["status"] == "incomplete"
    finally:
        storage.close()


@pytest.mark.parametrize("pause", [0, .5])
def test_actual_bot_runtime_trades_three_contracts_without_live_requests(tmp_path, monkeypatch, pause):
    import run

    clock = HistoricalClock(START, END, timedelta(minutes=1), pause, sleeper=lambda _: None)
    timeline = MultiTimeframeScheduler(["1m"], clock=clock)
    cache = MarketDataCache(source, timeline, clock=clock, freshness_tolerance_bars=2)
    session = HistoricalRunSession(clock)
    selection = selected()
    if pause:
        selection = replace(selection, catalog=selection.catalog[::-1]).planned(START, END, {}, 2)
    session.contract_selection = selection
    bindings = {"NG": [Assignment("binding", "macd_rsi_stoch", "levels_rr", "raw", timeframe="1m")]}
    monkeypatch.setattr(run, "_family_warmup", lambda family: {"1m": 2})
    monkeypatch.setattr(run, "FUTURE_STRATEGIES", bindings)
    monkeypatch.setattr(run, "RISK_LIMITS", {"max_qty": 1, "commission": 1, "slippage": 0})
    # Проверяется переход контрактов без расписания торгов: сессионная защита выключена.
    monkeypatch.setattr(run, "INTRADAY_SETTINGS", IntradaySettings(enabled=False))
    monkeypatch.setattr(run, "TRADE_MANAGEMENT_PROFILES", {"levels_rr": {
        "target_R": [3, 4], "shares": [.5, .5], "min_stop_atr": 0,
        "min_stop_ticks": 1, "stop_beyond_bar": 0, "max_stop_atr": None}})
    monkeypatch.setattr(run, "trading_enabled", lambda: False)  # Уточнение пользователя: история всё равно симулирует.
    monkeypatch.setattr(run, "_token_client_context", lambda: pytest.fail("История обратилась к боевому API"))
    monkeypatch.setattr("src.scheduler.clock.system_now", lambda: datetime(2040, 1, 1))
    bus = EventBus()
    runtime = run._build_runtime(selection.instruments, cache, bus, tmp_path, clock=clock, session=session)

    class EntryStrategy:
        def compute(self, frame):
            return frame

        def decide(self, frame, timeframe=None):
            return Decision(SignalType.BUY, float(frame["high"].iloc[-1]),
                            bar_time=frame["datetime"].iloc[-1])

    class Context:
        def get_context(self, instrument, timeframe, frame=None):
            base = 100+(int(instrument.uid[-1])-1)*50
            return MarketContext(TrendResult(TrendDirection.UP, .8),
                                 [SRLevel(base-4, SRType.SUPPORT, 3, "support")], base)

    try:
        bot = TradingBot(selection.instruments, bus, {"macd_rsi_stoch": object()}, cache, timeline,
            strategy_factory=lambda *args, **kwargs: EntryStrategy(), future_strategies=bindings,
            trade_manager=runtime.trade_manager, action_executor=runtime.action_executor,
            context_cache=Context(), post_tick=runtime.post_tick, run=session,
            contract_lifecycle=runtime.contract_lifecycle)
        bot.run()
        result = collect_result(runtime.storage)
        assert session.covered
        assert result.trades == 3
        assert result.realized_pnl == 60 and result.net_pnl == 55
        assert result.balance == 100055 and result.equity == 100085
        facts = collect_rollover(runtime.storage, selection)["transitions"]
        assert [event["old_flat_at"] for event in facts] == ["2026-01-01T10:06:00Z", "2026-01-01T10:11:00Z"]
        assert runtime.storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 5
    finally:
        runtime.storage.close()


def test_two_independent_families_and_share_use_one_account(tmp_path):
    ng = selected()
    br_cards = tuple(replace(card, uid=card.uid.replace("uid-", "br-"), family="BR",
        ticker=card.ticker.replace("NG", "BR"), short_name=card.short_name.replace("NG", "BR"),
        expiration_date=card.expiration_date+timedelta(minutes=2)) for card in ng.catalog)
    stock = Instrument("SBER", "SBER", "share", "SBER", "share")
    mixed = HistorySelection((stock,), ("NG", "BR"), ng.catalog+br_cards).planned(START, END, {}, 2)
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path, selection=mixed)
    try:
        p = replace(plan("stock", ng.catalog[0], clock.now()), assignment_id="share-binding", instrument_id="share",
                    reference_entry=D(10), stop_price=D(6))
        manager.submit_plan(p, OpenTrade("stock-entry", "stock", 0, "entry", 1, EntryOrderType.LIMIT, D(10)),
                            price_step=D(1), step_cost=D(1))
        manager.dispatch(clock.now())
        created = set()
        while clock.now() < END:
            step(clock, cache, broker, manager, lifecycle, metadata)
            for family, state in lifecycle.states.items():
                key = (family, state["index"])
                if state["phase"] == "ACTIVE" and key not in created and clock.now() < END:
                    card = lifecycle.chains[family][state["index"]].card
                    trade = f"{family}-{state['index']}"
                    p = replace(plan(trade, card, clock.now()), assignment_id=f"binding-{family}")
                    manager.submit_plan(p, OpenTrade(f"entry-{trade}", trade, 0, "entry", 1, EntryOrderType.LIMIT, p.reference_entry),
                                        price_step=D(1), step_cost=D(10))
                    manager.dispatch(clock.now())
                    created.add(key)
        result = collect_result(storage)
        assert result.trades == 7
        assert result.realized_pnl == 120 and result.fees == 11 and result.balance == 100109
        assert result.equity == 100139
        assert storage.load_trade("stock").state.quantity == 1
        assert {family: state["index"] for family, state in lifecycle.states.items()} == {"NG": 2, "BR": 2}
        assert len(collect_rollover(storage, mixed)["transitions"]) == 4
    finally:
        storage.close()


def test_delayed_close_skips_expired_target_without_fake_trade(tmp_path):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("delayed", card, clock.now())
        manager.submit_plan(p, OpenTrade("delayed-entry", "delayed", 0, "entry", 1, EntryOrderType.LIMIT, D(100)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        step(clock, cache, broker, manager, lifecycle, metadata)
        clock.jump_to(START+timedelta(minutes=11))
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        assert lifecycle.states["NG"]["phase"] == "DRAINING"
        step(clock, cache, broker, manager, lifecycle, metadata)
        assert lifecycle.states["NG"]["index"] == 2
        facts = collect_rollover(storage, lifecycle.selection)["transitions"]
        assert [fact["status"] for fact in facts] == ["skipped", "active"]
        assert storage.connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        assert collect_result(storage).net_pnl == 28
    finally:
        storage.close()


def test_failed_transition_commit_retries_from_durable_state(tmp_path, monkeypatch):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        clock.jump_to(START+timedelta(minutes=5))
        original = storage.transaction

        @contextmanager
        def failed_transaction():
            with original() as connection:
                yield connection
                raise RuntimeError("write failed")

        monkeypatch.setattr(storage, "transaction", failed_transaction)
        with pytest.raises(RuntimeError, match="write failed"):
            lifecycle.before_tick(clock.now())
        assert lifecycle.states["NG"]["phase"] == "ACTIVE"
        assert storage.connection.execute("SELECT COUNT(*) FROM rollover_transitions").fetchone()[0] == 0
        monkeypatch.setattr(storage, "transaction", original)
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        assert storage.connection.execute("SELECT COUNT(*) FROM rollover_transitions").fetchone()[0] == 1
        assert collect_result(storage).balance == 100000
    finally:
        storage.close()


def test_uid_keeps_reused_ticker_candles_and_positions_separate(tmp_path):
    original = selected()
    cards = tuple(replace(card, ticker="NGU6") for card in original.catalog)
    selection = HistorySelection(families=("NG",), catalog=cards).planned(START, END, {}, 2)
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path, selection=selection)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        for card in cards:
            cache.prepare_history(card.instrument, "1m", 2, card.first_trade_date)
        assert len({cache.frame_for(card.instrument, "1m")["close"].iloc[-1] for card in cards}) == 3
        assert len({card.instrument.contract_id for card in cards}) == 3
        assert len(metadata) == 3
    finally:
        storage.close()


def test_late_fill_during_cancellation_holds_barrier_until_factual_close(tmp_path):
    clock, cache, storage, _, manager, lifecycle, metadata, _ = setup(tmp_path)

    class LateBroker:
        def __init__(self):
            self.events = []

        def register_trade(self, p):
            pass

        def contract_for(self, key):
            return metadata[key]

        def submit(self, action, now):
            timestamp = as_aware(now)
            if isinstance(action, CancelEntry):
                self.events.extend([
                    ExecutionEvent("entry-cancel", "late-entry", "late-entry", "late", ExecutionStatus.CANCEL,
                        0, None, D(0), timestamp, "contract-rollover"),
                    ExecutionEvent("late-fill", "late-entry", "late-entry", "late", ExecutionStatus.FILL,
                        1, D(99), D(1), as_aware(START+timedelta(minutes=4)), "entry", fee_source=FeeSource.CONFIGURED),
                ])
            status = ExecutionStatus.CANCEL if isinstance(action, CancelEntry) else ExecutionStatus.ACK
            return ExecutionEvent(f"{action.command_id}:ack", action.command_id, action.command_id, action.trade_id,
                status, 0, None, D(0), timestamp, action.reason if isinstance(action, CancelEntry) else "next-bar")

        def drain_addressed_events(self):
            events, self.events = self.events, []
            return events

    broker = LateBroker()
    manager._broker = broker
    try:
        clock.advance()
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        card = lifecycle.chains["NG"][0].card
        p = plan("late", card, clock.now())
        manager.submit_plan(p, OpenTrade("late-entry", "late", 0, "entry", 1, EntryOrderType.LIMIT, D(99)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        clock.jump_to(START+timedelta(minutes=5))
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        assert lifecycle.states["NG"]["phase"] == "DRAINING"
        assert storage.load_trade("late").state.quantity == 1
        close_id, = storage.connection.execute("SELECT command_id FROM orders WHERE action_type='CLOSE'").fetchone()
        clock.advance()
        manager.consume(ExecutionEvent("late-close", close_id, close_id, "late", ExecutionStatus.FILL,
            1, D(103), D(1), as_aware(clock.now()), "contract-rollover", fee_source=FeeSource.CONFIGURED))
        lifecycle.after_execution(clock.now())
        assert lifecycle.states["NG"]["phase"] == "ACTIVE"
        assert collect_result(storage).net_pnl == 38
        assert not manager.consume(ExecutionEvent("late-fill", "late-entry", "late-entry", "late", ExecutionStatus.FILL,
            1, D(99), D(1), as_aware(START+timedelta(minutes=4)), "entry", fee_source=FeeSource.CONFIGURED))
        assert collect_result(storage).net_pnl == 38
    finally:
        storage.close()


def test_rejected_close_is_retried_without_duplicate_active_command(tmp_path, monkeypatch):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    original = broker.submit
    rejected = []

    def submit(action, now):
        if isinstance(action, CloseTrade) and not rejected:
            rejected.append(action.command_id)
            return ExecutionEvent(f"{action.command_id}:reject", action.command_id, action.command_id, action.trade_id,
                                  ExecutionStatus.REJECT, 0, None, D(0), as_aware(now), "venue-reject")
        return original(action, now)

    monkeypatch.setattr(broker, "submit", submit)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("retry", card, clock.now())
        manager.submit_plan(p, OpenTrade("retry-entry", "retry", 0, "entry", 1, EntryOrderType.LIMIT, D(100)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        while clock.now() < START+timedelta(minutes=7):
            step(clock, cache, broker, manager, lifecycle, metadata)
        assert len(rejected) == 1
        assert storage.connection.execute("SELECT COUNT(*) FROM orders WHERE action_type='CLOSE'").fetchone()[0] == 2
        assert storage.load_trade("retry", include_terminal=True).state.quantity == 0
        assert collect_result(storage).net_pnl == 28
        assert lifecycle.states["NG"]["phase"] == "ACTIVE"
    finally:
        storage.close()


def test_interruption_during_rollover_reports_open_residue(tmp_path):
    import run

    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("abort", card, clock.now())
        manager.submit_plan(p, OpenTrade("abort-entry", "abort", 0, "entry", 1, EntryOrderType.LIMIT, D(100)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        step(clock, cache, broker, manager, lifecycle, metadata)
        clock.jump_to(START+timedelta(minutes=5))
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        session = HistoricalRunSession(clock)
        session.contract_lifecycle = lifecycle
        run._finish_history(storage, session, cache, cache._timeline, tmp_path)
        result = json.loads((tmp_path / "report.json").read_text())
        assert result["crashed"] and not result["covered"]
        assert result["rollover"]["transitions"][0]["status"] == "incomplete"
        assert result["result"]["open_positions"] == ["NG-1.26 1"]
    finally:
        storage.close()


def test_partial_close_keeps_barrier_and_remaining_quantity(tmp_path):
    clock, cache, storage, _, manager, lifecycle, metadata, _ = setup(tmp_path)

    class AckBroker:
        def register_trade(self, p):
            pass

        def contract_for(self, key):
            return metadata[key]

        def submit(self, action, now):
            return ExecutionEvent(action.command_id+":ack", action.command_id, action.command_id, action.trade_id,
                ExecutionStatus.ACK, 0, None, D(0), as_aware(now), "next-bar")

    manager._broker = AckBroker()
    try:
        clock.advance()
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        card = lifecycle.chains["NG"][0].card
        p = plan("two", card, clock.now())
        manager.submit_plan(p, OpenTrade("two-entry", "two", 0, "entry", 2, EntryOrderType.LIMIT, D(100)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        manager.consume(ExecutionEvent("two-fill", "two-entry", "two-entry", "two", ExecutionStatus.FILL,
            2, D(100), D(2), as_aware(clock.now()), "entry", fee_source=FeeSource.CONFIGURED))
        clock.jump_to(START+timedelta(minutes=5))
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        close_id, = storage.connection.execute("SELECT command_id FROM orders WHERE action_type='CLOSE'").fetchone()
        clock.advance()
        manager.consume(ExecutionEvent("close-half", close_id, close_id, "two", ExecutionStatus.PARTIAL,
            1, D(103), D(1), as_aware(clock.now()), "contract-rollover", fee_source=FeeSource.CONFIGURED))
        lifecycle.after_execution(clock.now())
        assert lifecycle.states["NG"]["phase"] == "DRAINING"
        assert storage.load_trade("two").state.quantity == 1
        assert storage.connection.execute("SELECT COUNT(*) FROM orders WHERE action_type='CLOSE'").fetchone()[0] == 1
        clock.advance()
        manager.consume(ExecutionEvent("close-rest", close_id, close_id, "two", ExecutionStatus.FILL,
            1, D(103), D(1), as_aware(clock.now()), "contract-rollover", fee_source=FeeSource.CONFIGURED))
        lifecycle.after_execution(clock.now())
        assert lifecycle.states["NG"]["phase"] == "ACTIVE"
        assert collect_result(storage).net_pnl == 56
    finally:
        storage.close()


def test_protective_stop_on_retirement_is_visible_in_transition_costs(tmp_path):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("protected", card, clock.now())
        manager.submit_plan(p, OpenTrade("protected-entry", "protected", 0, "entry", 1, EntryOrderType.LIMIT, D(100)),
                            price_step=D(1), step_cost=D(10))
        manager.dispatch(clock.now())
        while clock.now() < START+timedelta(minutes=5):
            step(clock, cache, broker, manager, lifecycle, metadata)
        clock.advance()
        lifecycle.before_tick(clock.now())
        broker.track_bar(as_aware(START+timedelta(minutes=5)), {card.uid: (95, 94, 96, 95)}, metadata)
        for event in broker.drain_addressed_events():
            manager.consume(event)
        lifecycle.after_execution(clock.now())
        fact, = collect_rollover(storage, lifecycle.selection)["transitions"]
        assert fact["closed_trades"] == ["protected"]
        assert fact["exit_fees"] == "1"
        assert [row["action_type"] for row in fact["exits"]] == ["STOP"]
        assert collect_result(storage).net_pnl == -52
    finally:
        storage.close()


def test_ack_delivery_failure_cannot_leave_claimed_increase_on_rollover(tmp_path):
    clock, cache, storage, broker, manager, lifecycle, metadata, _ = setup(tmp_path)
    try:
        step(clock, cache, broker, manager, lifecycle, metadata)
        card = lifecycle.chains["NG"][0].card
        p = plan("lost-ack", card, clock.now())
        action = OpenTrade("lost-ack-entry", "lost-ack", 0, "entry", 1, EntryOrderType.LIMIT, D(99))
        manager.submit_plan(p, action, price_step=D(1), step_cost=D(10))
        storage.claim_outbox()
        broker.submit(action, as_aware(clock.now()))  # ACK ещё не записан в торговую базу.
        clock.jump_to(START+timedelta(minutes=5))
        lifecycle.before_tick(clock.now())
        lifecycle.after_execution(clock.now())
        assert not manager.contract_has_obligations(card.uid)
        assert storage.requeue_claimed_outbox() == 0
        assert storage.connection.execute("SELECT status FROM outbox WHERE command_id='lost-ack-entry'").fetchone()[0] == "DISCARDED"
        assert storage.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    finally:
        storage.close()
