"""Offline lifecycle replay through production planning, outbox, execution and CSV.

The real NG slice is a regression input, not a reconstruction of old journal
statistics. Synthetic boundaries and deliberate partial broker facts are labelled
in sidecar metadata. Strategy fixtures and their case selection are independent.
"""

import csv
import hashlib
import json
import sqlite3
from time import perf_counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.broker.journal_broker import JournalBroker
from src.broker.port import ExecutionEvent, ExecutionStatus, FeeSource
from src.bot.run_session import HistoricalRunSession
from src.bot.trading_bot import _EntryCandidate
from src.data.cache import MarketDataCache
from src.instruments import Instrument
from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.portfolio import ContractMeta, PositionManager
from src.strategies.contracts import Decision, SignalType
from src.scheduler.clock import HistoricalClock
from src.scheduler.timing import MultiTimeframeScheduler
from src.trade_journal.storage import Storage
from src.trade_journal.schema import SCHEMA_V9_SQL
from src.trade_management.actions import CloseTrade, OpenTrade, action_payload
from src.trade_management.manager import TradeManager, _plan_payload, _profile_payload
from src.trade_management.models import PlanEconomics, ProfileSnapshot, TargetPlan, TradePlan

D = Decimal
DATA = Path(__file__).parent / "data"
CASES = [(folder.name, scenario, side)
         for folder in sorted(DATA.iterdir()) if (folder / "lifecycle_metadata.json").exists()
         for scenario in json.loads((folder / "lifecycle_metadata.json").read_text())["scenarios"]
         for side in ("BUY", "SELL")]
GEOMETRY = {"min_stop_atr": 0, "min_stop_ticks": 1, "stop_beyond_bar": 0, "max_stop_atr": None}
PROFILE = {"buffer_ticks": 1, "target_R": (1, 2), "shares": (0.5, 0.5), "min_be_r": 1.5,
           "max_adds": 2, "add_fraction": 0.5, **GEOMETRY}


def _load(case, scenario, side):
    folder = DATA / case
    metadata = json.loads((folder / "lifecycle_metadata.json").read_text())
    assert hashlib.sha256((folder / "candles.csv").read_bytes()).hexdigest() == metadata["sha256"]
    assert len(list(folder.glob("candles.csv"))) == 1
    frame = pd.read_csv(folder / "candles.csv")
    frame["datetime"] = pd.to_datetime(frame["datetime"], utc=True)
    if side == "SELL" and case == "LIFECYCLE_1m":
        original_high, original_low = frame["high"].copy(), frame["low"].copy()
        for column in ("open", "close"):
            frame[column] = 200-frame[column]
        frame["high"], frame["low"] = 200-original_low, 200-original_high
    if scenario == "gap-stop":
        frame.loc[5, ["open", "low", "high", "close"]] = (94, 90, 150, 100) if side == "BUY" else (106, 50, 110, 100)
    if scenario == "close-open":
        frame.loc[5, ["low", "high"]] = (95, 150) if side == "BUY" else (50, 105)
    if scenario == "history-gap":
        frame.loc[3:, "datetime"] += pd.Timedelta(days=2)
    if case == "LIFECYCLE_1m":
        start = frame.iloc[0]["datetime"]
        warm = pd.DataFrame({"datetime": pd.date_range(start-pd.Timedelta(minutes=30), periods=30, freq="min"),
                             "open": [100]*30, "high": [101]*30, "low": [99]*30, "close": [100]*30, "volume": [100]*30})
        frame = pd.concat([warm, frame], ignore_index=True)
        anchor = 30
    else:
        anchor = 60
        frame = frame.iloc[:181].copy()
    return metadata, frame, anchor


def _broker(metadata, clock):
    broker = JournalBroker(None, PositionManager(float(metadata["balance"]), 2), [], clock=lambda: clock[0])
    contract = ContractMeta(metadata["instrument_id"], float(metadata["price_step"]), float(metadata["step_cost"]),
                            float(metadata["go"]), float(metadata["go"]))
    broker.set_contracts({contract.ticker: contract})
    broker.set_names({contract.ticker: metadata["contract"]})
    return broker, contract


def _manager(storage, broker, metadata, clock, max_qty):
    return TradeManager(storage, broker, initial_balance=D(metadata["balance"]), clock=lambda: clock[0],
        max_qty=max_qty, commission=metadata["commission"], slippage=metadata["slippage"], portfolio_pct=metadata["portfolio_pct"],
        min_risk_cost_ratio=metadata["min_risk_cost_ratio"], min_net_payoff=metadata["min_net_payoff"], max_slippage_r=metadata["max_slippage_r"],
        profiles_config={"levels_rr": PROFILE, "atr_trend": {**PROFILE, "shares": (0.25, 0.25), "atr_period": 14, "initial_k": 2, "trail_k": 2},
                         "pattern_targets": {"buffer_ticks": 1, "shares": (0.5, 0.5), "min_be_r": 1.5, **GEOMETRY}})


def _consume(manager, events, storage, *, repeat=False, rollback=False):
    rolled_back = False
    for event in events:
        if rollback and not rolled_back and event.status in {ExecutionStatus.FILL, ExecutionStatus.PARTIAL}:
            before = storage.connection.execute("SELECT balance,fees FROM account").fetchall()
            storage.connection.execute("CREATE TEMP TRIGGER abort_replay BEFORE INSERT ON fills BEGIN SELECT RAISE(ABORT,'replay rollback'); END")
            with pytest.raises(sqlite3.IntegrityError, match="replay rollback"):
                manager.consume(event)
            assert storage.connection.execute("SELECT balance,fees FROM account").fetchall() == before
            assert storage.connection.execute("SELECT 1 FROM fills WHERE execution_id=?", (event.execution_id,)).fetchone() is None
            storage.connection.execute("DROP TRIGGER abort_replay")
            rolled_back = True
        assert manager.consume(event)
        if repeat:
            assert not manager.consume(event)


def _legacy_snapshot(path, metadata, clock, side):
    """An actual v9 pending market order, migrated by production Storage."""
    direction = 1 if side == "BUY" else -1
    plan = TradePlan("trade", "assignment", metadata["instrument_id"], side, "legacy-signal", D(100), D(100-direction*4),
        (TargetPlan("tp-1", D(100+direction*4), D("0.5")), TargetPlan("tp-2", D(100+direction*8), D("0.5"))),
        ProfileSnapshot("levels_rr", "1", PROFILE), clock[0], timeframe="1m",
        economics=PlanEconomics(10, D(80), D(120), D(40), D(1)))
    payload = _plan_payload(plan)
    for key in ("algorithm_version", "cost_snapshot", "admission_snapshot", "entry_order_type", "requested_quantity", "price_step"):
        payload.pop(key)
    action = OpenTrade("legacy-entry", "trade", 0, "legacy-entry", 10)
    stamp = clock[0].isoformat()
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V9_SQL)
    connection.execute("PRAGMA user_version=9")
    connection.execute("INSERT INTO export_state(export_id,updated_at) VALUES(1,?)", (stamp,))
    connection.execute("INSERT INTO trades VALUES('trade','assignment',?,'legacy-signal',?, ?,?,'ENTRY_PENDING',0,'{}',?,?,'1','2')",
                       (metadata["instrument_id"], side, json.dumps(payload, sort_keys=True), json.dumps(_profile_payload(plan), sort_keys=True), stamp, stamp))
    connection.execute("INSERT INTO positions VALUES('trade',?,0,NULL,'0','0','0',?)", (side, stamp))
    connection.execute("INSERT INTO protection VALUES('trade',NULL,NULL,NULL,NULL,?)", (stamp,))
    for index, target in enumerate(plan.targets):
        connection.execute("INSERT INTO targets VALUES(?,'trade',?,?,0,0,'PENDING')", (target.target_id, index, str(target.price)))
    connection.execute("INSERT INTO account VALUES(1,'100000','100000','0','0','0',?)", (stamp,))
    connection.execute("INSERT INTO outbox VALUES('legacy-entry','trade',?,'PENDING',?,NULL)", (json.dumps(action_payload(action)), stamp))
    connection.execute("INSERT INTO orders VALUES('legacy-entry','trade','legacy-entry','OPEN','PENDING',10,0,NULL,?,?)", (stamp, stamp))
    connection.execute("INSERT INTO reservations VALUES('legacy-reserve','trade','legacy-entry','80','0','80','0','ACTIVE',?,?)", (stamp, stamp))
    connection.commit()
    connection.close()


def _replay(tmp_path, case, scenario, side, *, restart=False, repeat=False, rollback=False, metrics=None):
    metadata, frame, anchor = _load(case, scenario, side)
    period = timedelta(minutes=15 if metadata["timeframe"] == "15m" else 1)
    clock = [frame.iloc[anchor]["datetime"].to_pydatetime()+period]
    broker, contract = _broker(metadata, clock)
    if scenario == "legacy":
        _legacy_snapshot(tmp_path / "trades.sqlite3", metadata, clock, side)
    storage = Storage(tmp_path / "trades.sqlite3", journal_path=tmp_path / "event.csv", positions_path=tmp_path / "summary.csv")
    storage.set_names({contract.ticker: metadata["contract"]})
    max_qty = 1 if scenario == "one" else 3 if scenario == "odd" else 10
    manager = _manager(storage, broker, metadata, clock, max_qty)
    profile = "atr_trend" if scenario == "trailing" else "pattern_targets" if scenario == "pattern" else "levels_rr"
    assignment = SimpleNamespace(id="assignment", management=profile, priority=10, timeframe=metadata["timeframe"], filter_profile="raw")
    instrument = SimpleNamespace(ticker=contract.ticker, short_name=metadata["contract"])
    entry = D(str(frame.iloc[anchor]["close"]))
    distance = D(metadata["price_step"])*D(metadata["stop_ticks"])
    context = MarketContext(trend=TrendResult(direction=TrendDirection.UP, strength=0.6), sr_levels=[
        SRLevel(entry-distance+D(metadata["price_step"]), SRType.SUPPORT, 2, "support"),
        SRLevel(entry+distance-D(metadata["price_step"]), SRType.RESISTANCE, 2, "resistance")], current_price=entry)
    references = None
    if scenario == "pattern":
        references = {"pattern_id": "boundary", "c": D(97 if side == "BUY" else 103),
                      "d": D(112 if side == "BUY" else 88), "time_available": clock[0]}
    decision = Decision(SignalType(side), float(entry), event_id="trade", available_at=clock[0], idea_references=references)
    if scenario == "legacy":
        manager.restore()
        admission = None
    else:
        admission = manager.actions_for_signal(assignment, decision, instrument, frame.iloc[:anchor+1], context, timeframe=metadata["timeframe"])
        assert not admission.rejections and admission.actions[0].quantity == max_qty
    immutable = storage.connection.execute("SELECT plan_json FROM trades WHERE trade_id='trade'").fetchone()[0]
    manager.dispatch(clock[0])
    if scenario == "over-budget":
        with storage.transaction() as connection:
            connection.execute("UPDATE account SET balance='4000',equity='4000'")
    budgets = []
    for index in range(anchor+1, len(frame)):
        row = frame.iloc[index]
        opening = row["datetime"].to_pydatetime()
        clock[0] = opening+period
        values = tuple(float(row[column]) for column in ("open", "low", "high", "close"))
        if scenario == "partial" and index == anchor+1:
            action = admission.actions[0]
            event = ExecutionEvent("entry-partial", action.command_id, action.command_id, "trade", ExecutionStatus.PARTIAL,
                4, entry, D(6), opening, "partial-broker-fact", fee_source=FeeSource.CONFIGURED)
            _consume(manager, [event], storage, repeat=repeat, rollback=rollback)
        else:
            broker.track_bar(opening, {contract.ticker: values}, {contract.ticker: contract}, bar_times={contract.ticker: opening})
            _consume(manager, broker.drain_addressed_events(), storage, repeat=repeat, rollback=rollback)
        manager.observe_bars({contract.ticker: values}, {contract.ticker: opening}, timeframe=metadata["timeframe"])
        manager.mark_to_market({contract.ticker: D(str(row["close"]))})
        budgets.append(manager.portfolio_diagnostics())
        if scenario == "add" and index == anchor+2:
            max_qty = 15
            manager._max_qty = max_qty
            add = Decision(SignalType(side), float(row["close"]), event_id="add-signal", available_at=clock[0])
            assert manager.actions_for_signal(assignment, add, instrument, frame.iloc[:index+1], context, timeframe=metadata["timeframe"]).actions
        manager.manage(instrument, (assignment,), frame.iloc[:index+1], context, timeframe=metadata["timeframe"], now=clock[0])
        if scenario == "close-open" and index == anchor+4:
            state = storage.load_trade("trade").state
            manager.submit_action(CloseTrade("close-open", "trade", state.state_revision, "boundary-signal-exit"), market_close=row["close"])
        manager.dispatch(clock[0])
        if restart or scenario == "partial" and index == anchor+1:
            storage.close()
            storage = Storage(tmp_path / "trades.sqlite3", journal_path=tmp_path / "event.csv", positions_path=tmp_path / "summary.csv")
            broker, contract = _broker(metadata, clock)
            manager = _manager(storage, broker, metadata, clock, max_qty)
            manager.restore()
            if repeat:
                broker.track_bar(opening, {contract.ticker: values}, {contract.ticker: contract}, bar_times={contract.ticker: opening})
                _consume(manager, broker.drain_addressed_events(), storage, repeat=True)
    assert storage.connection.execute("SELECT plan_json FROM trades WHERE trade_id='trade'").fetchone()[0] == immutable
    account = storage.connection.execute("SELECT balance,equity,realized_pnl,fees,net_realized_pnl FROM account").fetchone()
    position = storage.connection.execute("SELECT quantity,average_price,realized_pnl,fees FROM positions WHERE trade_id='trade'").fetchone()
    executions = storage.connection.execute("SELECT o.action_type,f.quantity,f.price,f.fee,f.fee_source FROM fills f JOIN orders o ON o.order_id=f.order_id ORDER BY f.executed_at,f.rowid").fetchall()
    with (tmp_path / "summary.csv").open(newline="", encoding="utf-8") as stream:
        card = next(csv.DictReader(stream))
    with (tmp_path / "event.csv").open(newline="", encoding="utf-8") as stream:
        event_rows = list(csv.DictReader(stream))
    measurements = storage.connection.execute("SELECT initial_stop_distance,max_quantity,coverage FROM trade_measurements WHERE trade_id='trade'").fetchone()
    observations = storage.connection.execute("SELECT timeframe,bar_id,low,high,observed_price,coverage FROM trade_market_observations ORDER BY observed_at,bar_id").fetchall()
    summary = {"account": list(account), "position": list(position), "executions": [list(row) for row in executions],
               "card": card, "events_csv": event_rows, "measurements": list(measurements),
               "observations": [list(row) for row in observations]}
    if case == "LIFECYCLE_1m":
        expected_fees = D(45) if scenario == "add" else D(0) if scenario == "legacy" else D(3)*max_qty
        assert D(account[3]) == expected_fees  # independent 1.5 × (all entry+exit contracts)
        assert D(account[2])-D(account[3]) == D(account[4])
        if scenario in {"full", "partial", "history-gap", "over-budget"}:
            assert D(account[2]) == D(160) and D(account[4]) == D(130)
        if scenario == "close-open":
            assert D(account[2]) == D(120) and D(account[4]) == D(90)
        if scenario == "gap-stop":
            assert D(account[2]) == D(0) and D(account[4]) == D(-30)
        if scenario == "one":
            assert D(account[2]) == D(20) and D(account[4]) == D(17)
        if scenario == "odd":
            assert D(account[2]) == D(52) and D(account[4]) == D(43)
        if scenario == "legacy":
            assert D(account[2]) == D(120) and card["Версия алгоритма"] == "legacy-v1"
            assert card["Полнота издержек"] == "неизвестны"
        assert position[0] == 0
    if scenario == "over-budget":
        assert budgets[0]["risk_excess"] > 0
        assert all(action.split(":")[0] not in {"REDUCE", "CLOSE"} for action, *_ in executions)
    if scenario == "history-gap":
        assert card["Полнота экстремумов"] == "частичные"
    assert "Trade ID" not in card and card["Контракт"] == metadata["contract"]
    if metrics is not None:
        metrics.update(observation_count=len(observations), observation_json_bytes=len(json.dumps(observations).encode()),
                       database_bytes=(tmp_path / "trades.sqlite3").stat().st_size)
    storage.close()
    return summary


@pytest.mark.parametrize("case,scenario,side", CASES)
def test_lifecycle_matches_golden_and_restart_repeat_rollback(tmp_path, request, case, scenario, side):
    baseline_dir, resumed_dir = tmp_path / "baseline", tmp_path / "resumed"
    baseline_dir.mkdir()
    resumed_dir.mkdir()
    actual = _replay(baseline_dir, case, scenario, side)
    restored = _replay(resumed_dir, case, scenario, side, restart=True, repeat=True, rollback=True)
    assert restored == actual
    path = DATA / case / "lifecycle_expected.json"
    key = f"{scenario}:{side}"
    expected = json.loads(path.read_text()) if path.exists() else {}
    if request.config.getoption("--update-snapshots"):
        expected[key] = actual
        path.write_text(json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True)+"\n")
    else:
        for number, (want, got) in enumerate(zip(expected[key]["events_csv"], actual["events_csv"])):
            assert want == got, {"event_row": number, "differences": {field: (want.get(field), got.get(field)) for field in got if want.get(field) != got.get(field)}}
        assert expected[key] == actual


def test_fixed_gap_fixture_uses_bounded_history_search_and_honest_report():
    """Real cache/session path: two-day jump, then an exhausted seven-day search."""
    metadata, frame, anchor = _load("LIFECYCLE_1m", "history-gap", "BUY")
    start = frame.iloc[anchor]["datetime"].to_pydatetime().replace(tzinfo=None)
    end = start+timedelta(days=12)
    data = frame.copy()
    data["datetime"] = data["datetime"].dt.tz_localize(None)
    windows = []

    def loader(ticker, instrument_type, timeframe, start_date=None, end_date=None, **kwargs):
        selected = data
        if start_date is not None:
            selected = selected[selected["datetime"] >= pd.Timestamp(start_date).tz_localize(None)]
        if end_date is not None:
            selected = selected[selected["datetime"] <= pd.Timestamp(end_date).tz_localize(None)]
            windows.append((start_date, end_date))
        return selected.copy(), "fixture-uid"

    clock = HistoricalClock(start, end, timedelta(minutes=1), 0, sleeper=lambda seconds: None)
    timeline = MultiTimeframeScheduler(["1m"], clock=clock)
    session = HistoricalRunSession(clock)
    cache = MarketDataCache(loader=loader, timeline=timeline, clock=clock)
    instrument = Instrument(metadata["contract"], metadata["instrument_id"], "future", metadata["contract"])
    cache.frame_for(instrument, "1m")
    seen = set()
    for _ in range(30):
        if session.should_stop():
            break
        clock.advance()
        cache.refresh_if_new_candle("1m")
        cache.close_tick()
        observed = cache.frame_for(instrument, "1m")
        if not observed.empty:
            stamp = pd.Timestamp(observed.iloc[-1]["datetime"])
            if stamp >= start:
                seen.add(stamp)
        gap = cache.take_pending_gap()
        if gap is not None:
            session.mark_data_gap(gap)
            timeline.reanchor()
        if cache.data_exhausted:
            session.mark_data_exhausted(cache.data_exhaustion)
    expected = set(data.iloc[anchor:]["datetime"])
    assert seen == expected  # no interpolated candles inside the missing interval
    assert cache.gaps == 1 and cache.missed_bars == 2880
    assert session.covered is False and session.should_stop()
    assert "после него данные не проверялись" in session.stop_reason()
    assert windows and all((until-since) <= timedelta(days=7) for since, until in windows)


@pytest.mark.parametrize("mode", ["priority", "margin"])
def test_fixed_fixture_admission_order_and_reservation_resize(mode, tmp_path):
    metadata, frame, anchor = _load("LIFECYCLE_1m", "full", "BUY")
    metadata = {**metadata, "balance": "50000" if mode == "priority" else "20000", "commission": "0", "slippage": "0"}
    period = timedelta(minutes=1)
    clock = [frame.iloc[anchor]["datetime"].to_pydatetime()+period]
    broker, _ = _broker(metadata, clock)
    contracts = ({"first": ContractMeta("first", 1, 100, 0, 0), "second": ContractMeta("second", 1, 37.5, 0, 0)}
                 if mode == "priority" else {"first": ContractMeta("first", 1, 25, 8000, 8000)})
    broker.set_contracts(contracts)
    names = {"first": "NG-11.26", "second": "Si-12.26"}
    with Storage(tmp_path / "budget.sqlite3") as storage:
        storage.set_names(names)
        manager = _manager(storage, broker, metadata, clock, 3 if mode == "priority" else 5)
        context = MarketContext(trend=TrendResult(direction=TrendDirection.UP, strength=0.6),
                                sr_levels=[SRLevel(D(97), SRType.SUPPORT, 2, "support")], current_price=D(100))
        candidates = []
        for identifier, priority in (("second", 5), ("first", 10)) if mode == "priority" else (("first", 10),):
            assignment = SimpleNamespace(id=identifier, management="levels_rr", priority=priority, timeframe="1m", filter_profile="raw")
            instrument = SimpleNamespace(ticker=identifier, short_name=names[identifier])
            decision = Decision(SignalType.BUY, 100, event_id=identifier, available_at=clock[0])
            candidates.append(_EntryCandidate(assignment, decision, instrument, frame.iloc[:anchor+1], context, "1m"))
        quantities = []
        for candidate in sorted(candidates, key=_EntryCandidate.sort_key):
            admission = manager.actions_for_signal(candidate.assignment, candidate.decision, candidate.instrument,
                                                  candidate.frame, candidate.context, timeframe="1m")
            assert not admission.rejections
            quantities.append(admission.actions[0].quantity)
        assert quantities == ([2, 1] if mode == "priority" else [2])
        state = manager._budget_state(storage.connection)
        assert state.pending_risk == (D(950) if mode == "priority" else D(200))
        assert state.pending_margin == (D(0) if mode == "priority" else D(16000))
        manager.dispatch(clock[0])
        row = frame.iloc[anchor+1]
        values = tuple(float(row[c]) for c in ("open", "low", "high", "close"))
        broker.track_bar(row["datetime"].to_pydatetime(), {key: values for key in contracts}, contracts)
        _consume(manager, broker.drain_addressed_events(), storage, repeat=True)
        state = manager._budget_state(storage.connection)
        assert state.pending_risk == state.pending_margin == 0
        assert state.open_risk == (D(950) if mode == "priority" else D(200))
        assert state.open_margin == (D(0) if mode == "priority" else D(16000))


@pytest.mark.parametrize("case,scenario", [("LIFECYCLE_1m", "full"), ("NG_15m", "real")])
def test_replay_observation_size_and_elapsed_time(case, scenario, tmp_path):
    metrics = {}
    started = perf_counter()
    _replay(tmp_path, case, scenario, "BUY", metrics=metrics)
    elapsed = perf_counter()-started
    assert metrics["observation_count"] > 0 and metrics["observation_json_bytes"] > 0
    print(f"{case}: observations={metrics['observation_count']}, JSON bytes={metrics['observation_json_bytes']}, replay={elapsed:.3f}s")
