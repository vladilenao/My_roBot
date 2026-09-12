import numpy as np
import pandas as pd

from src.strategies import get_strategy
from src.strategies.contracts import SignalType
from src.strategies.ma_cloud_rsi_macd_strategy import (
    DEFAULT_CONFIG,
    MaCloudRsiMacdStrategy,
)
from src.strategies.registry import strategy_names


LONG_ENTRY_SERIES = np.concatenate(
    [
        np.linspace(110, 90, 44),
        [95, 99, 103, 107, 111, 115, 119, 123, 127, 131, 135, 139, 143, 147, 151, 155],
    ]
)

SHORT_ENTRY_SERIES = np.concatenate(
    [
        np.linspace(90, 110, 44),
        [105, 101, 97, 93, 89, 85, 81, 77, 73, 69, 65, 61, 57, 53, 49, 45],
    ]
)


def _df_from_close(close: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": [1000] * len(close),
        }
    )


def _decide_through(strategy: MaCloudRsiMacdStrategy, close: np.ndarray):
    decisions = []
    for i in range(41, len(close)):
        df = _df_from_close(close[: i + 1])
        ta = strategy.compute(df)
        decisions.append(strategy.decide(ta))
    return decisions


def _ta_scenario(
    close: float,
    high: float | None = None,
    low: float | None = None,
    sma_fast: float | None = None,
    sma_slow: float | None = None,
    ma_signal: int = 0,
    rsi: float = 50.0,
    rsi_signal: int = 0,
    macd_signal: int = 0,
    rows: int = 5,
) -> pd.DataFrame:
    """Детерминированный DataFrame для контроля логики decide()."""
    high = close + 1 if high is None else high
    low = close - 1 if low is None else low
    sma_fast = close if sma_fast is None else sma_fast
    sma_slow = close if sma_slow is None else sma_slow
    data = {
        "close": [close] * rows,
        "high": [high] * rows,
        "low": [low] * rows,
        "sma_fast": [sma_fast] * rows,
        "sma_slow": [sma_slow] * rows,
        "ma_cloud_signal": [ma_signal] * rows,
        "rsi": [rsi] * rows,
        "rsi_signal": [rsi_signal] * rows,
        "macd_zero_signal": [macd_signal] * rows,
    }
    return pd.DataFrame(data)


class TestMaCloudRsiMacdStrategy:
    def test_registered_and_discoverable(self):
        assert "ma_cloud_rsi_macd" in strategy_names()
        strategy = get_strategy("ma_cloud_rsi_macd", config=DEFAULT_CONFIG)
        assert isinstance(strategy, MaCloudRsiMacdStrategy)

    def test_required_history(self):
        strat = MaCloudRsiMacdStrategy(DEFAULT_CONFIG)
        assert strat.required_history() == 41

    def test_compute_adds_columns(self):
        close = np.linspace(100, 120, 60)
        df = _df_from_close(close)
        strat = MaCloudRsiMacdStrategy()
        ta = strat.compute(df)
        assert "ma_cloud_signal" in ta.columns
        assert "rsi_signal" in ta.columns
        assert "macd_zero_signal" in ta.columns

    def test_default_config_name(self):
        assert DEFAULT_CONFIG.name == "ma_cloud_rsi_macd"
        assert DEFAULT_CONFIG.strategy_window == 1

    # ── Вход (Entry) ─────────────────────────────────────────────
    def test_long_entry_on_third_indicator(self):
        strat = MaCloudRsiMacdStrategy()
        decisions = _decide_through(strat, LONG_ENTRY_SERIES)
        entries = [d for d in decisions if d.signal_type == SignalType.BUY]
        assert entries, "Expected at least one BUY entry"
        assert entries[0].action == "entry"
        assert entries[0].exit_reason is None
        assert entries[0].exit_contracts is None

    def test_short_entry_on_third_indicator(self):
        strat = MaCloudRsiMacdStrategy()
        decisions = _decide_through(strat, SHORT_ENTRY_SERIES)
        entries = [d for d in decisions if d.signal_type == SignalType.SELL]
        assert entries, "Expected at least one SELL entry"
        assert entries[0].action == "entry"

    def test_hold_when_only_two_indicators(self):
        close = np.linspace(100, 110, 60)
        strat = MaCloudRsiMacdStrategy()
        decisions = _decide_through(strat, close)
        assert all(d.action is None and d.exit_reason is None for d in decisions)

    def test_flat_data_gives_hold(self):
        close = np.full(60, 100.0)
        strat = MaCloudRsiMacdStrategy()
        decisions = _decide_through(strat, close)
        assert all(d.signal_type == SignalType.HOLD for d in decisions)

    def test_no_entry_while_long_open(self):
        """Пока открыт Long, повторный вход не даётся."""
        strat = MaCloudRsiMacdStrategy()
        decisions = _decide_through(strat, LONG_ENTRY_SERIES)
        entries = [d for d in decisions if d.signal_type == SignalType.BUY]
        assert len(entries) == 1

    def test_long_entry_sets_one_contract(self):
        strat = MaCloudRsiMacdStrategy()
        _decide_through(strat, LONG_ENTRY_SERIES)
        assert strat._long_contracts == 1

    # ── Окно сигнала (6 свечей) ──────────────────────────────────
    def test_no_entry_when_signals_span_more_than_window(self):
        """RSI@48, MA@53, MACD@57 — разброс 9 свечей > окна 6: входа нет."""
        close = np.concatenate([np.linspace(110, 90, 40), np.linspace(90, 130, 60)])
        strat = MaCloudRsiMacdStrategy()
        strat.compute(_df_from_close(close))  # прогрев
        decisions = _decide_through(strat, close)
        assert all(d.action != "entry" for d in decisions)
        assert strat._long_contracts == 0

    def test_pending_indicators_cleared_on_expiry(self):
        strat = MaCloudRsiMacdStrategy()
        strat._pending_long = [-30, -10]
        ta = _ta_scenario(100.0)
        strat.decide(ta)
        assert strat._pending_long == []

    # ── Добор (scale-in) ──────────────────────────────────────────
    def test_scale_in_long_on_cloud_retest(self):
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 1
        ta = _ta_scenario(
            close=118.0,
            low=115.0,  # Low внутри облака [115, 120]
            high=119.0,
            sma_fast=120.0,
            sma_slow=115.0,  # облако: 115..120
            ma_signal=0,
            rsi_signal=0,
            macd_signal=0,
        )
        decision = strat.decide(ta)
        assert decision.signal_type == SignalType.BUY
        assert decision.action == "scale_in"
        assert strat._long_contracts == 2

    def test_scale_in_short_on_cloud_retest(self):
        strat = MaCloudRsiMacdStrategy()
        strat._short_contracts = 1
        ta = _ta_scenario(
            close=122.0,
            high=125.0,  # High внутри облака [120, 125]
            low=121.0,
            sma_fast=120.0,
            sma_slow=125.0,  # облако: 120..125
            rsi_signal=0,
            macd_signal=0,
        )
        decision = strat.decide(ta)
        assert decision.signal_type == SignalType.SELL
        assert decision.action == "scale_in"
        assert strat._short_contracts == 2

    def test_no_scale_in_when_no_position(self):
        strat = MaCloudRsiMacdStrategy()
        ta = _ta_scenario(
            close=118.0,
            low=115.0,
            sma_fast=120.0,
            sma_slow=115.0,
        )
        decision = strat.decide(ta)
        assert decision.action is None

    # ── Выход (exit) ──────────────────────────────────────────────
    def test_exit_one_contract_below_ma40(self):
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 1
        ta = _ta_scenario(
            close=108.0,
            sma_fast=114.0,
            sma_slow=110.0,  # close < MA40 → полный выход
        )
        decision = strat.decide(ta)
        assert decision.signal_type == SignalType.SELL
        assert decision.exit_reason == "close_below_ma40"
        assert decision.exit_contracts is None
        assert strat._long_contracts == 0

    def test_one_contract_ignores_ma10_exit(self):
        """Правило: 1 контракт — выход только под MA40, MA10 игнорируется."""
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 1
        ta = _ta_scenario(
            close=112.0,
            low=115.0,  # low выше облака → нет scale-in
            high=116.0,
            sma_fast=114.0,  # close < MA10, но
            sma_slow=110.0,  # close > MA40
        )
        decision = strat.decide(ta)
        assert decision.exit_reason is None
        assert decision.signal_type == SignalType.HOLD
        assert strat._long_contracts == 1

    def test_partial_exit_below_ma10_when_two_contracts(self):
        """below_ma10 partial only reachable when close < min(MA10, MA40) — downtrend order."""
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 2
        ta = _ta_scenario(
            close=108.0,  # below MA10=110 and below cloud_low=110
            sma_fast=110.0,  # MA10 (lower in downtrend)
            sma_slow=115.0,  # MA40 (higher): cloud=[110, 115]
        )
        decision = strat.decide(ta)
        assert decision.exit_reason == "close_below_ma10"
        assert decision.exit_contracts == 1
        assert strat._long_contracts == 1

    def test_full_exit_below_ma40_when_two_contracts(self):
        """Full exit fires for >1 contracts only when partial tier doesn't match.
        With downtrend order (MA10<MA40): close < MA10 triggers partial first.
        Test that partial fires on first encounter, full on second bar."""
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 2
        ta1 = _ta_scenario(
            close=108.0,
            sma_fast=110.0,  # MA10 < MA40
            sma_slow=115.0,  # MA40
        )
        d1 = strat.decide(ta1)
        assert d1.exit_reason == "close_below_ma10"
        assert strat._long_contracts == 1
        # Второй бар: 1 контракт → ниже MA40 → full exit
        ta2 = _ta_scenario(
            close=108.0,
            sma_fast=110.0,
            sma_slow=115.0,
        )
        d2 = strat.decide(ta2)
        assert d2.exit_reason == "close_below_ma40"
        assert strat._long_contracts == 0

    def test_full_exit_direct_when_one_contract(self):
        """1 контракт — только below MA40 exit."""
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 1
        ta = _ta_scenario(
            close=108.0,
            sma_fast=110.0,
            sma_slow=115.0,  # close < MA40
        )
        decision = strat.decide(ta)
        assert decision.exit_reason == "close_below_ma40"
        assert decision.exit_contracts is None
        assert strat._long_contracts == 0

    def test_partial_exit_inside_cloud_when_two_contracts(self):
        """Выход внутри облака: downtrend order MA10 < MA40, close в облаке."""
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 2
        ta = _ta_scenario(
            close=112.0,
            low=111.0,
            sma_fast=110.0,  # MA10 < MA40 (downtrend)
            sma_slow=115.0,  # MA40: облако [110, 115]
        )
        decision = strat.decide(ta)
        assert decision.exit_reason == "close_inside_cloud"
        assert decision.exit_contracts == 1
        assert strat._long_contracts == 1

    def test_short_exit_one_contract_above_ma40(self):
        strat = MaCloudRsiMacdStrategy()
        strat._short_contracts = 1
        ta = _ta_scenario(
            close=112.0,
            sma_fast=106.0,
            sma_slow=110.0,  # close > MA40 → полный выход Short
        )
        decision = strat.decide(ta)
        assert decision.signal_type == SignalType.BUY
        assert decision.exit_reason == "close_above_ma40"
        assert strat._short_contracts == 0

    # ── Двусторонность ────────────────────────────────────────────
    def test_long_exit_does_not_touch_short(self):
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 2
        strat._short_contracts = 1
        ta = _ta_scenario(
            close=112.0,
            sma_fast=110.0,  # MA10 < MA40 (downtrend)
            sma_slow=115.0,  # MA40: облако [110, 115]
        )
        decision = strat.decide(ta)
        assert decision.exit_reason == "close_inside_cloud"
        assert strat._long_contracts == 1  # Long уменьшился
        assert strat._short_contracts == 1  # Short не тронут

    def test_short_exit_does_not_touch_long(self):
        strat = MaCloudRsiMacdStrategy()
        strat._long_contracts = 1
        strat._short_contracts = 1
        ta = _ta_scenario(
            close=112.0,
            sma_fast=106.0,
            sma_slow=110.0,  # Short: полный выход (close > MA40)
        )
        decision = strat.decide(ta)
        assert decision.exit_reason == "close_above_ma40"
        assert strat._short_contracts == 0
        assert strat._long_contracts == 1  # Long не тронут
