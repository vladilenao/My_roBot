"""Инструмент занят привязкой: встречный вход и вход другой привязки в ту же сторону (trade-management: «Владелец и жизненный цикл сделки»)."""

from decimal import Decimal
from types import SimpleNamespace

from src.market_context.models import MarketContext, SRLevel, SRType, TrendDirection, TrendResult
from src.strategies.contracts import Decision, SignalType
from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade, OpenTrade
from src.trade_management.manager import TradeManager, _exposure_key
from src.trade_management.models import SignalAdmission, rejection_reason

from tests.unit.trade_management.test_signal_pipeline import BAR0, INSTRUMENT, LIMITS, PROFILES, _FillBroker, _frame

NG_NEXT = SimpleNamespace(ticker="NGX6", short_name="NG-11.26")
SI = SimpleNamespace(ticker="SIZ6", short_name="Si-12.26")


def _assignment(assignment_id):
    return SimpleNamespace(id=assignment_id, strategy="macd_rsi_stoch", management="levels_rr",
                           filter_profile="raw", priority=0, timeframe="15m")


def _decision(assignment_id, signal_type):
    return Decision(signal_type=signal_type, price=100.0, bar_time=BAR0,
                    event_id=f"{assignment_id}:{signal_type.value}", available_at=BAR0, timeframe="15m")


def _context():
    return MarketContext(trend=TrendResult(direction=TrendDirection.FLAT, strength=0.0),
                         sr_levels=[SRLevel(97.0, SRType.SUPPORT, 2, "s1"), SRLevel(103.0, SRType.RESISTANCE, 2, "r1")],
                         current_price=100.0)


def _manager(storage):
    return TradeManager(storage, _FillBroker(), initial_balance=Decimal("100000"), profiles_config=PROFILES,
                        risk_limits=LIMITS, max_qty=4)


def _admit(manager, assignment_id, signal_type, instrument=INSTRUMENT):
    return manager.actions_for_signal(_assignment(assignment_id), _decision(assignment_id, signal_type), instrument,
                                      _frame([100.0] * 25), _context(), timeframe="15m")


def _trades(storage):
    return storage.connection.execute("SELECT assignment_id, side, phase FROM trades ORDER BY created_at").fetchall()


def _traces(storage, algorithm="portfolio.opposite-exposure"):
    return storage.connection.execute(
        "SELECT reason, outcome, assignment_id, signal_id FROM calculations WHERE algorithm=?", (algorithm,)
    ).fetchall()


def _busy(storage):
    return _traces(storage, "portfolio.instrument-busy")


class TestOppositeEntryIsRejected:
    def test_open_long_blocks_short_of_another_assignment(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            storage.set_names({"NGV6": "NG-10.26"})
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)
            before = _trades(storage)

            admission = _admit(manager, "harmonic", SignalType.SELL)

            assert admission.actions == ()
            assert admission.plan is None
            assert [(r.code, r.message) for r in admission.rejections] == [
                ("opposite-exposure", "по NG-10.26 уже есть встречная покупка")]
            assert _trades(storage) == before
            assert [row[0] for row in before] == ["flat"]
            assert _traces(storage) == [("opposite-exposure", "REJECTED", "harmonic", "harmonic:SELL")]

    def test_pending_entry_blocks_opposite_entry(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            opening = _admit(manager, "flat", SignalType.SELL)
            assert isinstance(opening.actions[0], OpenTrade)

            admission = _admit(manager, "harmonic", SignalType.BUY)

            assert [(r.code, r.message) for r in admission.rejections] == [
                ("opposite-exposure", "уже есть встречная продажа")]
            assert [row[0] for row in _trades(storage)] == ["flat"]

    def test_other_contract_of_same_family_blocks(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)

            admission = _admit(manager, "harmonic", SignalType.SELL, instrument=NG_NEXT)

            assert [r.code for r in admission.rejections] == ["opposite-exposure"]

    def test_uid_contracts_use_persisted_short_names(self, tmp_path):
        june = SimpleNamespace(ticker="BRM5", short_name="BR-6.25", uid="uid-june")
        july = SimpleNamespace(ticker="BRN5", short_name="BR-7.25", uid="uid-july")
        with Storage(tmp_path / "trades.sqlite3") as storage:
            storage.set_names({"uid-june": "BR-6.25", "uid-july": "BR-7.25"})
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY, instrument=june)
            manager.dispatch(BAR0)

            admission = _admit(manager, "harmonic", SignalType.SELL, instrument=july)

            assert [(r.code, r.message) for r in admission.rejections] == [
                ("opposite-exposure", "по BR-6.25 уже есть встречная покупка")]


class TestSameDirectionOfAnotherAssignment:
    def test_open_long_blocks_long_of_another_assignment(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            storage.set_names({"NGV6": "NG-10.26"})
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)
            before = _trades(storage)

            admission = _admit(manager, "harmonic", SignalType.BUY)

            assert admission.actions == ()
            assert admission.plan is None
            assert [(r.code, r.message) for r in admission.rejections] == [
                ("instrument-busy", "по NG-10.26 уже ведёт сделку macd_rsi_stoch · 15m")]
            assert _trades(storage) == before
            assert _busy(storage) == [("instrument-busy", "REJECTED", "harmonic", "harmonic:BUY")]
            assert _traces(storage) == []

    def test_pending_entry_occupies_instrument(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            opening = _admit(manager, "flat", SignalType.SELL)
            assert isinstance(opening.actions[0], OpenTrade)

            admission = _admit(manager, "harmonic", SignalType.SELL)

            assert [r.code for r in admission.rejections] == ["instrument-busy"]
            assert [row[0] for row in _trades(storage)] == ["flat"]

    def test_other_contract_of_same_family_is_busy(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)

            admission = _admit(manager, "harmonic", SignalType.BUY, instrument=NG_NEXT)

            assert [r.code for r in admission.rejections] == ["instrument-busy"]

    def test_unknown_strategy_after_restart_is_named_by_assignment(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            storage.set_names({"NGV6": "NG-10.26"})
            _admit(_manager(storage), "flat", SignalType.BUY)

            admission = _admit(_manager(storage), "harmonic", SignalType.BUY)

            assert [(r.code, r.message) for r in admission.rejections] == [
                ("instrument-busy", "по NG-10.26 уже ведёт сделку привязка flat · 15m")]

    def test_owner_keeps_managing_own_trade(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)

            admission = _admit(manager, "flat", SignalType.BUY)

            assert "instrument-busy" not in [r.code for r in admission.rejections]
            assert _busy(storage) == []

    def test_closed_trade_frees_instrument(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)
            assert isinstance(_admit(manager, "flat", SignalType.SELL).actions[0], CloseTrade)
            manager.dispatch(BAR0)

            admission = _admit(manager, "harmonic", SignalType.BUY)

            assert admission.rejections == ()
            assert isinstance(admission.actions[0], OpenTrade)

    def test_open_trades_of_two_assignments_survive_and_block_third(self, tmp_path, monkeypatch):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            with monkeypatch.context() as legacy:  # пара, открытая до правила
                legacy.setattr(TradeManager, "_instrument_busy_rejection", lambda *args: None)
                _admit(manager, "flat", SignalType.BUY)
                _admit(manager, "harmonic", SignalType.BUY)
            manager.dispatch(BAR0)
            assert [row[0] for row in _trades(storage)] == ["flat", "harmonic"]

            third = _admit(manager, "triangle", SignalType.BUY)
            owner = _admit(manager, "harmonic", SignalType.SELL)

            assert [r.code for r in third.rejections] == ["instrument-busy"]
            assert [type(a).__name__ for a in owner.actions] == ["CloseTrade"]
            assert [row[0] for row in _trades(storage)] == ["flat", "harmonic"]


class TestCandidatesOfOneTick:
    """Кандидаты тика допускаются по очереди в порядке ранжирования (``_EntryCandidate.sort_key``)."""

    def test_first_admitted_candidate_takes_instrument(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)

            first = _admit(manager, "flat", SignalType.BUY)
            second = _admit(manager, "harmonic", SignalType.BUY)

            assert isinstance(first.actions[0], OpenTrade)
            assert [r.code for r in second.rejections] == ["instrument-busy"]
            assert _trades(storage) == [("flat", "BUY", "ENTRY_PENDING")]

    def test_rejected_candidate_does_not_take_instrument(self, tmp_path, monkeypatch):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            plan_entry = TradeManager._plan_entry

            def budget_refuses_flat(self, assignment, *args):
                if assignment.id == "flat":
                    return SignalAdmission(rejections=(rejection_reason("risk-budget"),))
                return plan_entry(self, assignment, *args)

            monkeypatch.setattr(TradeManager, "_plan_entry", budget_refuses_flat)

            first = _admit(manager, "flat", SignalType.BUY)
            second = _admit(manager, "harmonic", SignalType.BUY)

            assert [r.code for r in first.rejections] == ["risk-budget"]
            assert second.rejections == ()
            assert isinstance(second.actions[0], OpenTrade)


class TestEntriesThatRemainAllowed:
    def test_other_family_is_independent(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)

            admission = _admit(manager, "harmonic", SignalType.SELL, instrument=SI)

            assert admission.rejections == ()
            assert isinstance(admission.actions[0], OpenTrade)

    def test_closed_trade_no_longer_blocks(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)
            closing = _admit(manager, "flat", SignalType.SELL)
            assert isinstance(closing.actions[0], CloseTrade)
            manager.dispatch(BAR0)
            assert _trades(storage) == [("flat", "BUY", "CLOSED")]

            admission = _admit(manager, "harmonic", SignalType.SELL)

            assert admission.rejections == ()
            assert isinstance(admission.actions[0], OpenTrade)

    def test_own_opposite_signal_still_manages_own_trade(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            manager = _manager(storage)
            _admit(manager, "flat", SignalType.BUY)
            manager.dispatch(BAR0)

            admission = _admit(manager, "flat", SignalType.SELL)

            assert admission.rejections == ()
            assert [type(a).__name__ for a in admission.actions] == ["CloseTrade"]
            assert _traces(storage) == []


class TestExposureKey:
    def test_futures_share_family_key(self):
        assert _exposure_key("NGV6") == _exposure_key("NGX6", "NGX6", "NG-11.26") == "family:NG"
        assert _exposure_key("uid-1", "BRM5", "BR-6.25") == _exposure_key("uid-2", "uid-2", "BR-7.25", "BR")

    def test_unrecognised_instrument_compares_by_address(self):
        assert _exposure_key("SBER", "SBER", "SBER") == "SBER"
        assert _exposure_key("uid-x", "uid-x", None) == "uid-x"
