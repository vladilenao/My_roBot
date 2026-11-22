from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.assembly import contour
from src.scheduler.clock import HistoricalClock


def clock():
    return HistoricalClock(datetime(2026, 1, 1), datetime(2026, 1, 2), timedelta(minutes=1), 0)


def test_history_without_trading_section_has_initial_account_and_positive_costs(tmp_path, monkeypatch):
    monkeypatch.setattr(contour, "trading_enabled", lambda: False)
    monkeypatch.setattr(contour, "RISK_LIMITS", {})
    runtime = contour._build_runtime([], MagicMock(), MagicMock(), tmp_path, clock=clock())
    try:
        assert runtime.trade_manager is not None
        assert runtime.trade_manager._commission == Decimal("1.5")
        assert runtime.trade_manager._slippage == Decimal("1.0")
        assert runtime.storage.connection.execute("SELECT balance FROM account").fetchone()[0] == "100000"
    finally:
        runtime.storage.close()


def test_history_initialization_error_cannot_fall_back_to_notify_only(tmp_path, monkeypatch):
    monkeypatch.setattr(contour, "trading_enabled", lambda: False)

    def fail(*args, **kwargs):
        raise OSError("journal unavailable")

    monkeypatch.setattr("src.trade_journal.storage.Storage", fail)
    with pytest.raises(RuntimeError, match="исторический симулятор/журнал"):
        contour._build_runtime([], MagicMock(), MagicMock(), tmp_path, clock=clock())


def test_live_without_trading_section_stays_notify_only(tmp_path, monkeypatch):
    monkeypatch.setattr(contour, "trading_enabled", lambda: False)
    runtime = contour._build_runtime([], MagicMock(), MagicMock(), tmp_path)
    assert runtime.trade_manager is None and runtime.storage is None


def test_history_defers_journal_projections_but_live_exports_each_transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(contour, "trading_enabled", lambda: True)
    monkeypatch.setattr(contour, "RISK_LIMITS", {})
    history = contour._build_runtime([], MagicMock(), MagicMock(), tmp_path / "history", clock=clock())
    live = contour._build_runtime([], MagicMock(), MagicMock(), tmp_path / "live")
    try:
        assert history.storage._export_interval == contour.HISTORY_EXPORT_INTERVAL_SEC
        assert live.storage._export_interval is None
    finally:
        history.storage.close()
        live.storage.close()
