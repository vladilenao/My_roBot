"""Сигналы и недопуски по стратегиям в отчёте прогона (add-reconstructed-history-schedule D8)."""

import json
from types import SimpleNamespace

from src.history.admission_stats import SignalAdmissionStats, StrategyAdmission, trades_by_assignment
from src.history.report import collect_result, write_report
from src.trade_management.manager import SignalAdmission
from src.trade_management.models import rejection_reason
from tests.unit.test_history_report import _metrics, _storage_with_round_trip

MACD = SimpleNamespace(id="future-br-macd", timeframe="5m")
FLAT = SimpleNamespace(id="future-br-flat", timeframe="1h")


def signal(signal_id, signal_type="BUY"):
    return SimpleNamespace(event_id=signal_id, signal_type=SimpleNamespace(name=signal_type))


def rejected(*codes):
    return SignalAdmission(rejections=tuple(rejection_reason(code) for code in codes))


def test_signal_is_counted_once_with_its_first_rejection():
    stats = SignalAdmissionStats()
    stats.record(MACD, "5m", signal("s1"), rejected("session-schedule-unknown"))
    stats.record(MACD, "5m", signal("s1"), rejected("contract-expiring"))
    stats.record(MACD, "5m", signal("s2"), rejected("contract-expiring", "risk-budget"))

    (row,) = stats.rows({})

    assert (row.signals, row.trades, row.admitted) == (2, 0, 0)
    assert row.rejections == (("contract-expiring", 1), ("session-schedule-unknown", 1))


def test_admitted_signals_and_trades_are_separate():
    stats = SignalAdmissionStats()
    stats.record(FLAT, "1h", signal("s1"), SignalAdmission(actions=(object(),)))
    stats.record(FLAT, "1h", signal("s2"), rejected("filter-rejected"))

    stats.record(FLAT, "1h", signal("s3", "HOLD"), SignalAdmission())
    (row,) = stats.rows({"future-br-flat": 1})

    assert (row.signals, row.admitted, row.trades) == (2, 1, 1)
    assert not row.blocked_by_schedule


def test_blocked_by_schedule_needs_no_trades_and_half_of_signals():
    assert StrategyAdmission("a", "5m", 4, 0, 0, (("session-schedule-unknown", 2),)).blocked_by_schedule
    assert not StrategyAdmission("a", "5m", 5, 0, 0, (("session-schedule-unknown", 2),)).blocked_by_schedule
    assert not StrategyAdmission("a", "5m", 4, 1, 1, (("session-schedule-unknown", 3),)).blocked_by_schedule


def test_report_shows_table_and_warning(tmp_path):
    storage = _storage_with_round_trip(tmp_path)
    rows = (
        StrategyAdmission("future-br-flat", "1h", 13, 2, 2, (("filter-rejected", 11),)),
        StrategyAdmission("future-br-macd", "5m", 845, 0, 0, (("session-schedule-unknown", 845),)),
    )

    txt, js = write_report(tmp_path, _metrics(strategy_admissions=rows), collect_result(storage))
    text = txt.read_text(encoding="utf-8")
    payload = json.loads(js.read_text(encoding="utf-8"))

    assert "Сигналы и недопуски по стратегиям:" in text
    assert ("⚠ future-br-macd: 0 сделок — 845 из 845 сигналов отклонены: "
            "расписание торгов неизвестно, внутридневной вход запрещён") in text
    assert "future-br-flat (1h): сигналов 13, сделок 2" in text
    assert "filter-rejected — 11: сигнал отклонён фильтром привязки" in text
    assert "⚠ future-br-flat" not in text
    assert payload["strategy_admissions"][1] == {
        "assignment_id": "future-br-macd", "timeframe": "5m", "signals": 845, "admitted": 0,
        "trades": 0, "rejections": {"session-schedule-unknown": 845},
    }


def test_section_is_omitted_without_signals(tmp_path):
    storage = _storage_with_round_trip(tmp_path)

    txt, js = write_report(tmp_path, _metrics(), collect_result(storage))

    assert "Сигналы и недопуски" not in txt.read_text(encoding="utf-8")
    assert "strategy_admissions" not in json.loads(js.read_text(encoding="utf-8"))


def test_trades_are_counted_by_assignment(tmp_path):
    storage = _storage_with_round_trip(tmp_path)
    counts = trades_by_assignment(storage.connection)

    assert sum(counts.values()) == collect_result(storage).trades


def test_bot_records_admission_into_history_session():
    from src.bot.run_session import HistoricalRunSession
    from src.bot.trading_bot import TradingBot, _EntryCandidate
    from datetime import datetime, timedelta

    from src.scheduler.clock import HistoricalClock

    clock = HistoricalClock(datetime(2025, 4, 18, 9), datetime(2025, 4, 19), timedelta(minutes=1))
    session = HistoricalRunSession(clock)
    manager = SimpleNamespace(actions_for_signal=lambda *args, **kwargs: rejected("contract-expiring"))
    bot = SimpleNamespace(_trade_manager=manager, _run=session, _publish_signal=lambda *a, **k: None,
                          _dispatch_management_actions=lambda *a, **k: None,
                          _publish_rejection=lambda *a, **k: None)
    decision = signal("future-br-macd:BR:5m:2025-04-18T12:00:00:BUY")
    candidate = _EntryCandidate(SimpleNamespace(id="future-br-macd", timeframe="5m", filter_profile="raw"),
                                decision, None, None, None, "5m")

    TradingBot._admit_candidate(bot, candidate)
    TradingBot._admit_candidate(bot, candidate)

    (row,) = session.admission_stats.rows({})
    assert (row.assignment_id, row.signals, row.rejections) == ("future-br-macd", 1, (("contract-expiring", 1),))
