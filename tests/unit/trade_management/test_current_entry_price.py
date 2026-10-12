from decimal import Decimal
import pytest

from src.trade_management.economics import entry_price_deviation
from src.config_loader import ConfigError, load_config
from src.trade_management.manager import TradeManager
from src.trade_management.models import TradePlan, ProfileSnapshot
from src.trade_journal.storage import Storage
from src.strategies.contracts import Assignment, Decision, SignalType
from src.instruments import Instrument
from src.portfolio.models import ContractMeta
from src.trade_management.audit import calculation_trace, MeasuredValue
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch


@pytest.mark.parametrize("side,current,expected", [("BUY", 101.2, "0.3"), ("BUY", 101, "0.25"),
    ("BUY", 99, "0"), ("SELL", 98.8, "0.3"), ("SELL", 99, "0.25"), ("SELL", 101, "0")])
def test_deviation_is_adverse_only_and_independent_of_cost_estimate(side, current, expected):
    assert entry_price_deviation(side, current, 100, 96 if side == "BUY" else 104) == Decimal(expected)


def test_zero_r_has_no_valid_deviation():
    assert entry_price_deviation("BUY", 101, 100, 100) is None


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "true", '"0.25"'])
def test_entry_deviation_configuration_is_finite_nonnegative(tmp_path, value):
    path = tmp_path / "robot.toml"
    path.write_text("[trading.risk_limits]\nmax_entry_deviation_r=" + value)
    with pytest.raises(ConfigError, match="max_entry_deviation_r"):
        load_config({}, config_file=path)


def test_entry_deviation_default_and_zero_are_separate_from_slippage(tmp_path):
    path = tmp_path / "robot.toml"
    path.write_text("[trading.risk_limits]\nmax_entry_deviation_r=0\nmax_slippage_r=0.7")
    risk = load_config({}, config_file=path)["risk_limits"]
    assert risk["max_entry_deviation_r"] == 0
    assert risk["max_slippage_r"] == 0.7
    path.write_text("[trading.risk_limits]\nportfolio_pct=2")
    assert load_config({}, config_file=path)["risk_limits"]["max_entry_deviation_r"] == 0.25


@pytest.mark.parametrize("side,current,rejected", [("BUY", 101.2, True), ("BUY", 101, False), ("BUY", 99, False),
                                                   ("SELL", 98.8, True), ("SELL", 99, False), ("SELL", 101, False)])
def test_actual_admission_checks_price_before_reservation(tmp_path, side, current, rejected):
    from src.trade_management import pipeline
    import pandas as pd
    now = datetime(2026, 10, 12, 10)
    p = TradePlan("trade", "assignment", "SBER", side, "signal", Decimal(100),
                  Decimal(96 if side == "BUY" else 104), (), ProfileSnapshot("ma_cloud", "1", {}), now)
    trace = calculation_trace("test.plan", inputs={"entry": MeasuredValue(100, "price")},
        result=MeasuredValue("plan", "plan"), reason="planned", formula="test")
    profile = Mock()
    profile.plan_with_trace.return_value = p, trace
    snapshot = {"valid": True, "current_price": current, "price_time": now.isoformat(), "source": "broker",
                "previous_session_close": 98, "session_gap": current-98, "execution_ready": True}
    with Storage(tmp_path / "trades.sqlite3") as storage, patch.dict(pipeline.PROFILE_CLASSES, {"ma_cloud": lambda: profile}):
        manager = TradeManager(storage, Mock(), initial_balance=Decimal(100000), live_market_provider=lambda _: snapshot)
        sizing = Mock()
        manager._size_open_quantity = sizing
        # Проверка точной границы: проход дальше к sizing останавливаем sentinel.
        sizing.side_effect = RuntimeError("sizing reached")
        binding = Assignment(id="assignment", strategy="macd_rsi_stoch", management="ma_cloud", filter_profile="raw", timeframe="1h")
        meta = ContractMeta("SBER", 1, 1, 0, 0)
        df = pd.DataFrame({"datetime": pd.date_range(end=now, periods=50, freq="h"),
                          "open": [100]*50, "high": [101]*50, "low": [99]*50, "close": [100]*50})
        if rejected:
            result = manager._plan_entry(binding, Decision(SignalType(side), 100, event_id="signal"),
                Instrument("SBER", "SBER", "share"), df, None, meta, "1h")
            assert result.rejections[0].code == "entry-price-deviation"
            sizing.assert_not_called()
            assert storage.connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
            assert storage.connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        else:
            with pytest.raises(RuntimeError, match="sizing reached"):
                manager._plan_entry(binding, Decision(SignalType(side), 100, event_id="signal"),
                    Instrument("SBER", "SBER", "share"), df, None, meta, "1h")
