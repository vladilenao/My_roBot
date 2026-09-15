from datetime import timedelta

import numpy as np
import pandas as pd

from src.bot import TradingBot
from src.broker import JournalBroker
from src.broker.exec_adapter import BrokerExecutionAdapter
from src.decision import RiskManager
from src.execution import BrokerExecutionPort
from src.instruments import Instrument
from src.market_context.models import (
    MarketContext,
    TrendDirection,
    TrendResult,
)
from src.portfolio import ContractMeta, PositionManager
from src.strategies.contracts import Assignment
from src.strategies.ma_cloud_rsi_macd_strategy import (
    DEFAULT_CONFIG,
    MaCloudRsiMacdStrategy,
)
from src.trade_journal import OpType, TradeJournal

SI_META = ContractMeta(ticker="SIU6", price_step=1.0, step_cost=100.0, go_buy=5000.0, go_sell=5000.0)

# 51 бар (0..50): подтверждения RSI@45, MA@49, MACD@50 → вход на последнем баре (close=119).
LONG_ENTRY_SERIES = np.concatenate(
    [
        np.linspace(110.0, 90.0, 44),
        [95.0, 99.0, 103.0, 107.0, 111.0, 115.0, 119.0],
    ]
)


def _si_frame() -> pd.DataFrame:
    close = LONG_ENTRY_SERIES
    start = pd.Timestamp("2024-01-01")
    return pd.DataFrame(
        {
            "datetime": [start + pd.Timedelta(days=i) for i in range(len(close))],
            "open": close,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": [1000] * len(close),
        }
    )


class FakeTimeline:
    """Один тик (bootstrap), затем KeyboardInterrupt."""

    def __init__(self, timeframes=("5m",)):
        self.timeframes = tuple(timeframes)
        self.wait_calls = 0

    def wait_until_bar_published(self, bar_ready, poll_secs=1.0, timeout_secs=65.0, wait_boundary=True):
        self.wait_calls += 1
        if self.wait_calls > 1:
            raise KeyboardInterrupt
        return {tf for tf in self.timeframes if bar_ready(tf)}

    def grid(self, timeframe):
        return self

    def bar_close(self, bar_start):
        return bar_start + timedelta(minutes=5)

    def fallback_secs(self):
        return 1.0


class FakeCache:
    def __init__(self, frames=None):
        self.frames = frames or {}

    def refresh_if_new_candle(self, timeframe, force=False):
        pass

    def has_fresh_closed_bar(self, timeframe, now=None):
        return True

    def frame_for(self, instrument, timeframe):
        return self.frames.get((instrument.ticker, timeframe), pd.DataFrame())


class FakeContextCache:
    def __init__(self, frame):
        self.frame = frame

    def get_context(self, instrument, timeframe):
        return MarketContext(
            trend=TrendResult(TrendDirection.FLAT, 0.0),
            sr_levels=[],
            current_price=float(self.frame["close"].iloc[-1]),
        )


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def notify(self, message):
        self.messages.append(message)


def _make_bot(frame, execution, notifier):
    return TradingBot(
        instruments=[],
        notifier=notifier,
        strategy_map={"ma_cloud_rsi_macd": DEFAULT_CONFIG},
        data_cache=FakeCache(frames={("SIU6", "5m"): frame}),
        timeline=FakeTimeline(),
        execution=execution,
        strategy_factory=lambda name, config: MaCloudRsiMacdStrategy(config),
        share_strategies={},
        future_strategies={
            "SI": [
                Assignment(strategy="ma_cloud_rsi_macd", filter_profile="raw", timeframe="5m"),
                Assignment(strategy="ma_cloud_rsi_macd", filter_profile="triple_screen", timeframe="5m"),
            ]
        },
        heartbeat_every_ticks=None,
        context_cache=FakeContextCache(frame),
        signal_filter=None,
        risk_manager=RiskManager(),
    )


def test_si_5m_two_profiles_yield_one_entry_one_duplicate(tmp_path):
    """Контрактный e2e: SI 5m, привязки raw + triple_screen дают одинаковое решение;
    обе прошли фильтры → ровно один входящий ордер в журнале, второй — причина `duplicate`."""
    frame = _si_frame()
    journal = TradeJournal.created_on_init(tmp_path / "j.csv")
    manager = PositionManager(None, initial_deposit=100000, max_risk_pct=2.0)
    broker = JournalBroker(journal, manager, ["14:05", "19:00"])
    adapter = BrokerExecutionAdapter(broker, manager)
    adapter.set_contracts({"SIU6": SI_META}, names={"SIU6": "SI-12.26"})
    port = BrokerExecutionPort(adapter)

    bot = _make_bot(frame, port, FakeNotifier())
    bot._instruments = [Instrument("SI (доллар/рубль) — SI-12.26", "SIU6", "future", "SI-12.26")]
    bot.run()

    events = journal.events()
    entries = [e for e in events if e.op == OpType.ORDER.value]
    duplicates = [
        e for e in events if e.op == OpType.CANCEL.value and e.reason == "duplicate"
    ]
    assert len(entries) == 1, "должен быть ровно один входящий ордер"
    assert len(duplicates) == 1, "второй вход должен фиксироваться причиной duplicate"
    assert "raw" in entries[0].notes
    assert entries[0].contract == "SI-12.26", "пользовательские файлы используют короткое имя"
    assert len(manager.pending) == 1