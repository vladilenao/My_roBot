"""Фильтр привязки на допуске нового входа (change entry-filter-admission)."""

from decimal import Decimal
from types import SimpleNamespace

from src.decision.filter import SignalFilter
from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import Storage
from src.trade_management.actions import OpenTrade
from src.trade_management.manager import TradeManager

from tests.unit.trade_management.test_signal_pipeline import BAR0, INSTRUMENT, LIMITS, PROFILES, _FillBroker, _frame


def _assignment(filter_profile="basic_levels"):
    return SimpleNamespace(id="future-ng-macd", strategy="macd_rsi_stoch", management="levels_rr",
                           filter_profile=filter_profile, priority=0, timeframe="15m")


def _decision(signal_type=SignalType.BUY):
    return Decision(signal_type=signal_type, price=100.0, bar_time=BAR0, event_id=f"signal-{signal_type.value}",
                    available_at=BAR0, timeframe="15m")


def _context(direction):
    return MarketContext(trend=TrendResult(direction=direction, strength=0.6),
                         sr_levels=[SRLevel(97.0, SRType.SUPPORT, 2, "s1"), SRLevel(103.0, SRType.RESISTANCE, 2, "r1")],
                         current_price=100.0)


def _manager(storage, signal_filter=None):
    return TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"), profiles_config=PROFILES,
                        risk_limits=LIMITS, max_qty=4, signal_filter=signal_filter or SignalFilter())


def _traces(storage, algorithm):
    return storage.connection.execute(
        "SELECT reason, outcome, assignment_id, signal_id FROM calculations WHERE algorithm=?", (algorithm,)).fetchall()


def _admit(manager, decision, direction, assignment=None):
    return manager.actions_for_signal(assignment or _assignment(), decision, INSTRUMENT, _frame([100.0] * 25),
                                      _context(direction), timeframe="15m")


class TestBasicLevelsOnEntry:
    def test_buy_against_downtrend_is_rejected_before_planning(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admit(_manager(storage), _decision(SignalType.BUY), TrendDirection.DOWN)

            assert admission.actions == ()
            assert admission.plan is None
            assert [(r.code, r.message) for r in admission.rejections] == [
                ("filter-rejected", "покупка против нисходящего тренда")]
            assert storage.connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
            assert _traces(storage, "filter.basic_levels") == [
                ("trend-opposes-entry", "REJECTED", "future-ng-macd", "signal-BUY")]
            assert _traces(storage, "profile.levels_rr.plan") == []

    def test_sell_against_uptrend_names_the_trend(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admit(_manager(storage), _decision(SignalType.SELL), TrendDirection.UP)

            assert [(r.code, r.message) for r in admission.rejections] == [
                ("filter-rejected", "продажа против восходящего тренда")]

    def test_entry_along_trend_continues_to_planning(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admit(_manager(storage), _decision(SignalType.BUY), TrendDirection.UP)

            assert admission.rejections == ()
            assert isinstance(admission.actions[0], OpenTrade)
            assert _traces(storage, "filter.basic_levels") == [
                ("entry-allowed", "ACCEPTED", "future-ng-macd", "signal-BUY")]

    def test_raw_profile_does_not_block(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admit(_manager(storage), _decision(SignalType.BUY), TrendDirection.DOWN,
                               assignment=_assignment("raw"))

            assert admission.rejections == ()
            assert isinstance(admission.actions[0], OpenTrade)

    def test_without_market_context_filter_is_not_applied(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _manager(storage).actions_for_signal(
                _assignment(), _decision(SignalType.BUY), INSTRUMENT, _frame([100.0] * 25), None, timeframe="15m")

            assert all(r.code != "filter-rejected" for r in admission.rejections)
            assert _traces(storage, "filter.basic_levels") == []


class TestTripleScreenOnEntry:
    def test_unconfirmed_screen_is_named_in_rejection(self, tmp_path, monkeypatch):
        from src.decision.filters import PROFILES as REGISTRY
        from tests.unit.decision.test_triple_screen import _frame_from_close, _neutral_tail, _triple_filter, _uptrend

        frames = {"1d": _frame_from_close(_uptrend(), freq="1D"), "4h": _frame_from_close(_neutral_tail())}
        monkeypatch.setitem(REGISTRY, "triple_screen", _triple_filter(frames))
        with Storage(tmp_path / "trades.sqlite3") as storage:
            admission = _admit(_manager(storage), _decision(SignalType.BUY), TrendDirection.UP,
                               assignment=_assignment("triple_screen"))

            (rejection,) = admission.rejections
            assert rejection.code == "filter-rejected"
            assert rejection.message.startswith("экран 2 (Stochastic 4h) не подтверждает покупку: %K ")
            assert "экран 1" not in rejection.message
            assert admission.actions == ()
            assert _traces(storage, "filter.triple_screen") == [
                ("screen2-not-confirmed", "REJECTED", "future-ng-macd", "signal-BUY")]


class TestOwnedTradeSignals:
    def test_raw_opposite_signal_still_closes_own_trade(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            opening = _admit(manager, _decision(SignalType.BUY), TrendDirection.UP)
            manager.dispatch(BAR0)

            admission = _admit(manager, _decision(SignalType.SELL), TrendDirection.UP)

            assert admission.rejections == ()
            assert [(type(a).__name__, a.trade_id) for a in admission.actions] == [
                ("CloseTrade", opening.actions[0].trade_id)]
            assert [row[0] for row in _traces(storage, "filter.basic_levels")] == ["entry-allowed"]
