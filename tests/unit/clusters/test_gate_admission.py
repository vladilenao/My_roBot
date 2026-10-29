"""Проверки данных, трасса и место «Кластеров» в допуске (change add-cluster-filter, D3, D6, D8)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from src.clusters.gate import ClusterGate, collector_name
from src.clusters.settings import ClusterSettings
from src.clusters.store import ClusterEventStore, utc_text
from src.clusters.verdict import ClusterVerdict
from src.instruments.model import Instrument
from src.market_context.models import TrendDirection
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import Storage
from src.trade_management.actions import OpenTrade
from tests.unit.clusters.helpers import T, br_events, candles, history, signal_day
from tests.unit.trade_management.test_entry_filter_admission import _context, _manager
from tests.unit.trade_management.test_signal_pipeline import BAR0, INSTRUMENT, _frame

BR = Instrument("BR-11.26", "BRX6", "future", short_name="BR-11.26", uid="b2d0f863", family="BR")
NG = Instrument("NG-11.26", "NGX6", "future", short_name="NG-11.26", uid="ng-uid", family="NG")


def _fields(item, published=None):
    return {"category": item.category, "event_type": item.event_type, "event_id": item.event_id,
            "secid": item.secid, "ticker": item.source_ticker, "direction": item.direction or "",
            "signal_id": item.signal_id or "", "timestamp": utc_text(item.timestamp),
            "published_at": utc_text(published or item.published_at), "payload": json.dumps(dict(item.payload))}


def _store(tmp_path, events=None, quality=None):
    store = ClusterEventStore(tmp_path / "cluster-events.sqlite3")
    rows = [(f"{i}-0", _fields(item)) for i, item in enumerate(events if events is not None else br_events())]
    for i, (event_type, moment, secid) in enumerate(quality or ()):
        rows.append((f"q{i}-0", {"category": "data_quality", "event_type": event_type, "event_id": f"{i:064x}",
                                 "secid": secid, "ticker": "BR_CONTINUOUS", "timestamp": utc_text(moment),
                                 "published_at": utc_text(moment)}))
    store.write(rows)
    return store


def _gate(store, *, live=False, consumer=None, now=T):
    frame = candles(history(), signal_day())
    return ClusterGate(store, lambda instrument: frame, ClusterSettings(), consumer=consumer, live=live,
                       clock=lambda: now)


def _assignment(mode="shadow", filter_profile="raw"):
    return SimpleNamespace(id="future-br-macd", strategy="macd_rsi_stoch", management="levels_rr",
                           filter_profile=filter_profile, priority=0, timeframe="5m", clusters=mode)


def _decision(side=SignalType.SELL, bar_time=T):
    return Decision(signal_type=side, price=104.63, bar_time=pd.Timestamp(bar_time).tz_convert(None),
                    event_id=f"future-br-macd:BRX6:5m:{side.value}", available_at=None, timeframe="5m")


class TestGate:
    def test_br_veto(self, tmp_path):
        verdict = _gate(_store(tmp_path)).evaluate(_assignment(), _decision(), BR)

        assert (verdict.verdict, verdict.reason) == ("veto", "climax")

    def test_off_and_hold_give_no_verdict(self, tmp_path):
        gate = _gate(_store(tmp_path))

        assert gate.evaluate(_assignment("off"), _decision(), BR) is None
        assert gate.evaluate(_assignment(), _decision(SignalType.HOLD), BR) is None

    def test_consumer_lag_in_live(self, tmp_path):
        consumer = SimpleNamespace(last_read_at=T - timedelta(minutes=3))

        verdict = _gate(_store(tmp_path), live=True, consumer=consumer).evaluate(_assignment(), _decision(), BR)

        assert (verdict.verdict, verdict.reason) == ("unknown", "consumer_lag")
        assert verdict.message == "нет данных — чтение кластеров отстаёт на 180 с"

    def test_fresh_consumer_in_live(self, tmp_path):
        consumer = SimpleNamespace(last_read_at=T - timedelta(seconds=5))

        assert _gate(_store(tmp_path), live=True, consumer=consumer).evaluate(_assignment(), _decision(), BR).verdict == "veto"

    def test_contract_mismatch_names_short_contracts(self, tmp_path):
        moment = T - timedelta(minutes=5)
        ng = [e for e in br_events()][:1]
        ng = [type(ng[0])(**{**ng[0].__dict__, "secid": "NGV6", "source_ticker": "NG_CONTINUOUS",
                             "event_id": "e" * 64, "published_at": moment, "timestamp": moment})]

        verdict = _gate(_store(tmp_path, ng)).evaluate(_assignment(), _decision(), NG)

        assert (verdict.verdict, verdict.reason) == ("unknown", "contract_mismatch")
        assert verdict.message == "нет данных — коллектор ведёт NG-10.26, робот торгует NG-11.26"
        assert "NGV6" not in verdict.message and "NGX6" not in verdict.message

    def test_no_coverage(self, tmp_path):
        share = Instrument("SBER", "SBER", "share", short_name="SBER", uid="sber")

        verdict = _gate(_store(tmp_path)).evaluate(_assignment(), _decision(), share)

        assert (verdict.verdict, verdict.reason) == ("unknown", "no_coverage")

    def test_data_degraded(self, tmp_path):
        store = _store(tmp_path, quality=[("data_degraded", T - timedelta(minutes=2), "BRX6")])

        verdict = _gate(store).evaluate(_assignment(), _decision(), BR)

        assert (verdict.verdict, verdict.reason) == ("unknown", "data_degraded")

    def test_collector_name(self):
        assert collector_name("NGV6", "NG-11.26") == "NG-10.26"
        assert collector_name("SRZ6", "SBRF-12.26") == "SBRF-12.26"


class _FixedGate(ClusterGate):
    def __init__(self, verdict, enforce_level="veto"):
        super().__init__(None, lambda _: pd.DataFrame(), ClusterSettings(enforce_level=enforce_level))
        self._verdict = verdict

    def evaluate(self, assignment, decision, instrument):
        if assignment.clusters == "off":
            return None
        return ClusterVerdict(self._verdict, "climax", "вход на кульминации", assignment.clusters, {"sigma15_bps": 12.82})


def _admission(storage, gate, assignment, direction=TrendDirection.UP):
    manager = _manager(storage)
    manager._cluster_gate = gate
    decision = Decision(signal_type=SignalType.BUY, price=100.0, bar_time=BAR0, event_id="signal-BUY",
                        available_at=BAR0, timeframe="15m")
    return manager.actions_for_signal(assignment, decision, INSTRUMENT, _frame([100.0] * 25), _context(direction),
                                      timeframe="15m")


def _cluster_traces(storage):
    return storage.connection.execute(
        "SELECT outcome, assignment_id, signal_id, output_json FROM calculations WHERE algorithm='filter.cluster'").fetchall()


class TestAdmission:
    def test_shadow_veto_keeps_admission(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admission(storage, _FixedGate("veto"), _assignment("shadow"))

            assert isinstance(admission.actions[0], OpenTrade)
            assert admission.cluster.verdict == "veto"
            outcome, assignment_id, signal_id, output = _cluster_traces(storage)[0]
            assert (outcome, assignment_id, signal_id) == ("ACCEPTED", "future-br-macd", "signal-BUY")
            assert json.loads(output)["value"]["verdict"] == "veto"

    def test_enforce_veto_rejects(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admission(storage, _FixedGate("veto"), _assignment("enforce"))

            assert admission.plan is None and admission.actions == ()
            assert [(r.code, r.message) for r in admission.rejections] == [
                ("cluster-climax", "Кластеры: вход на кульминации")]
            assert _cluster_traces(storage)[0][0] == "REJECTED"
            assert storage.connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0

    def test_enforce_level_strong_only(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admission(storage, _FixedGate("veto", "veto_strong"), _assignment("enforce"))

            assert isinstance(admission.actions[0], OpenTrade)

    def test_unknown_in_enforce_passes(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admission(storage, _FixedGate("unknown"), _assignment("enforce"))

            assert isinstance(admission.actions[0], OpenTrade)

    def test_trend_filter_rejection_keeps_verdict(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admission(storage, _FixedGate("confirm"), _assignment("enforce", "basic_levels"),
                                   TrendDirection.DOWN)

            assert [r.code for r in admission.rejections] == ["filter-rejected"]
            assert admission.cluster.verdict == "confirm"
            assert len(_cluster_traces(storage)) == 1

    def test_off_records_nothing(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admission(storage, _FixedGate("veto"), _assignment("off"))

            assert admission.cluster is None
            assert _cluster_traces(storage) == []

    def test_owned_trade_signal_gets_no_verdict(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            first = _admission(storage, _FixedGate("veto"), _assignment("shadow"))
            assert isinstance(first.actions[0], OpenTrade)
            again = _admission(storage, _FixedGate("veto"), _assignment("shadow"))

            assert again.cluster is None
            assert len(_cluster_traces(storage)) == 1


def test_trace_inputs_reproduce_verdict(tmp_path):
    gate = _gate(_store(tmp_path))
    verdict = gate.evaluate(_assignment(), _decision(), BR)

    trace = gate.trace(verdict, _assignment(), _decision(), BR, "5m", rejected=False)

    assert trace.algorithm == "filter.cluster" and trace.algorithm_version == "clusters-v1"
    assert trace.inputs["bar_close"].value == "2026-10-15T13:50:00"
    assert trace.inputs["instrument_uid"].value == "b2d0f863"
    assert [item["label"] for item in trace.inputs["evidence"].value] == ["climax", "climax", "initiative", "climax"]
    assert trace.result.value["verdict"] == "veto"
    assert datetime.fromisoformat(trace.inputs["close_at_entry"].value["open_time_utc"].replace("Z", "+00:00")) \
        == datetime(2026, 10, 15, 13, 49, tzinfo=timezone.utc)
    assert Decimal(str(trace.inputs["price"].value)) == Decimal("104.63")


def test_live_instrument_without_uid_uses_pair_uid(tmp_path):
    """Live-выбор строит фьючерс без UID: свечи и трасса берут адрес пары из кэша."""
    from src.clusters.gate import store_candles

    frame = candles(history(), signal_day()).assign(is_complete=True)
    reads = []

    class Store:
        def current_frame(self, *, source, instrument_uid, interval):
            reads.append((source, instrument_uid, interval))
            return frame

    selected = Instrument("BR-11.26", "BRX6", "future", short_name="BR-11.26", uid=None, family="BR")
    pair_uid = {"BRX6": "b2d0f863"}.get
    gate = ClusterGate(_store(tmp_path), store_candles(Store(), "tbank_exchange", lambda i: pair_uid(i.ticker)),
                       ClusterSettings(), uid_for=lambda i: pair_uid(i.ticker))

    verdict = gate.evaluate(_assignment(), _decision(), selected)
    trace = gate.trace(verdict, _assignment(), _decision(), selected, "5m", rejected=False)

    assert verdict.verdict == "veto"
    assert reads == [("tbank_exchange", "b2d0f863", "1m")]
    assert trace.inputs["instrument_uid"].value == "b2d0f863"


def test_card_without_uid_reads_nothing():
    from src.clusters.gate import store_candles

    class Store:
        def current_frame(self, **kwargs):
            raise AssertionError("без UID журнал не читается")

    selected = Instrument("BR-11.26", "BRX6", "future", short_name="BR-11.26")

    assert store_candles(Store(), "tbank_exchange")(selected).empty
