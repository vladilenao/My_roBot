"""Глубина расчёта: окно стратегий одинаково в живом и историческом цикле (add-analysis-depth)."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from src.bot.trading_bot import TradingBot
from src.config import TIMEFRAMES
from src.data.analysis_window import analysis_window
from src.data.cache import MarketDataCache
from src.data.htf_provider import HtfFrameProvider
from src.decision.filters.triple_screen import TripleScreenParams
from src.events.bus import EventBus
from src.history.warmup import binding_depths, binding_requirements, obligation_requirements
from src.instruments import Instrument
from src.scheduler.clock import HistoricalClock
from src.scheduler.timing import MultiTimeframeScheduler
from src.strategies import get_strategy
from src.strategies.contracts import Assignment
from src.strategies.macd_rsi_stoch_strategy import DEFAULT_CONFIG as MACD_CONFIG

SBER = Instrument("SBER", "SBER", "share", "SBER", "SBER")
LADDER = tuple(TIMEFRAMES)
PROFILES = {"levels_rr": {}, "ma_cloud": {"ma_slow_period": 200}}


def _bars(count, *, end="2026-10-12 09:00", freq="1h"):
    stamps = pd.date_range(end=pd.Timestamp(end), periods=count, freq=freq)
    return pd.DataFrame({"datetime": stamps, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                         "volume": 1, "is_complete": True})


def _binding(filter_profile="basic_levels", management="levels_rr", timeframe="15m"):
    return Assignment(f"sber-{filter_profile}-{management}", "macd_rsi_stoch", management, filter_profile,
                      timeframe=timeframe)


def _strategy():
    return get_strategy("macd_rsi_stoch", MACD_CONFIG)


class GapCache:
    """Покрытие подтверждено только для суффиксов не длиннее ``covered``."""

    def __init__(self, covered=10**9):
        self.covered = covered
        self.calls = 0

    def coverage_for(self, instrument, timeframe, frame):
        self.calls += 1
        return not frame.empty and len(frame) <= self.covered


# ── минимум допуска и глубина ──

def test_depth_is_analysis_depth_for_work_and_higher_timeframes():
    assert binding_depths(_binding(), _strategy(), PROFILES, TripleScreenParams(), LADDER, 150) == {"15m": 150}
    triple = binding_depths(_binding("triple_screen"), _strategy(), PROFILES, TripleScreenParams(), LADDER, 150)
    assert triple == {"15m": 150, "4h": 150, "1d": 150}
    # Минимум допуска не меняется: допуск по-прежнему по 50, 18 и 37 свечам.
    assert binding_requirements(_binding("triple_screen"), _strategy(), PROFILES, TripleScreenParams(),
                                LADDER) == {"15m": 50, "4h": 18, "1d": 37}


def test_minimum_above_depth_wins():
    depths = binding_depths(_binding(management="ma_cloud"), _strategy(), PROFILES, TripleScreenParams(), LADDER, 150)
    assert depths == {"15m": 201}


def test_management_obligations_load_to_depth_but_not_execution_minute():
    plan = SimpleNamespace(timeframe="1h", profile=SimpleNamespace(parameters={"atr_period": 20}))
    trade = SimpleNamespace(plan=plan)
    assert obligation_requirements([trade]) == {"1h": 21, "1m": 1}
    assert obligation_requirements([trade], 150) == {"1h": 150, "1m": 1}


# ── окно расчёта ──

def test_window_is_tail_of_depth_when_covered():
    window = analysis_window(GapCache(), SBER, "1h", _bars(400), 50, 150)
    assert len(window) == 150 and window["datetime"].iloc[-1] == pd.Timestamp("2026-10-12 09:00")


def test_window_takes_all_history_shorter_than_depth():
    assert len(analysis_window(GapCache(), SBER, "1h", _bars(80), 50, 150)) == 80


def test_old_gap_inside_depth_shortens_window():
    cache = GapCache(covered=119)
    window = analysis_window(cache, SBER, "1h", _bars(400), 50, 150)
    assert len(window) == 119
    assert cache.calls <= 9  # поиск делением пополам, а не перебор


def test_window_within_minimum_skips_coverage():
    cache = GapCache()
    assert len(analysis_window(cache, SBER, "1m", _bars(300, freq="1min"), 1, 1)) == 1
    assert cache.calls == 0


# ── старшие ТФ triple_screen ──

class LiveCache(GapCache):
    live_session_enabled = True

    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        self.requested = []

    def prepare_live_history(self, instrument, timeframe, min_bars, *, max_close=None):
        self.requested.append(min_bars)
        return self.frame.copy()


class HistoryCache:
    def __init__(self, frame):
        self.frame = frame

    def ensure_loaded(self, instrument, timeframe):
        pass

    def frame_for(self, instrument, timeframe):
        return self.frame.copy()


def test_higher_timeframe_window_is_the_same_live_and_in_history():
    frame = _bars(400, freq="1D", end="2026-10-11")
    timeline = MultiTimeframeScheduler(["15m"], clock=lambda: datetime(2026, 10, 12, 9, 0))
    live_cache = LiveCache(frame)
    live = HtfFrameProvider(live_cache, timeline).frame_for(SBER, "1d", min_bars=37, depth=150)
    history = HtfFrameProvider(HistoryCache(frame), timeline).frame_for(SBER, "1d", min_bars=37, depth=150)
    assert len(live) == 150 and live["datetime"].tolist() == history["datetime"].tolist()
    assert live_cache.requested == [150]  # прогрев запрашивает глубину, допуск — по 37


def test_higher_timeframe_shorter_than_depth_is_not_rejected():
    frame = _bars(120, freq="7D", end="2026-10-05")
    timeline = MultiTimeframeScheduler(["1h"], clock=lambda: datetime(2026, 10, 12, 9, 0))
    assert len(HtfFrameProvider(LiveCache(frame), timeline).frame_for(SBER, "1w", min_bars=37, depth=150)) == 120


# ── исторический тик ──

def _history_bot():
    timeline = MultiTimeframeScheduler(["1h"], clock=lambda: datetime(2026, 10, 12, 10, 0))
    binding = _binding(timeframe="1h")
    bot = TradingBot([SBER], EventBus(), {"macd_rsi_stoch": MACD_CONFIG}, SimpleNamespace(), timeline,
                     share_strategies={"SBER": [binding]}, analysis_depth=150)
    return bot, binding


def test_history_tick_window_matches_live_window():
    bot, binding = _history_bot()
    frame = _bars(400)
    history = bot._analysis_window(frame, "1h", [binding])
    minimum = binding_requirements(binding, _strategy(), {"levels_rr": {}}, TripleScreenParams(), LADDER)["1h"]
    live = analysis_window(GapCache(), SBER, "1h", frame, minimum, max(minimum, 150))
    assert history["datetime"].tolist() == live["datetime"].tolist() and len(history) == 150


def test_history_management_window_covers_profile_warmup():
    bot, binding = _history_bot()
    assert len(bot._analysis_window(_bars(400), "1h", [binding], management=201)) == 201
    # Служебный 1m без привязок сопровождается всем кадром, как раньше.
    assert len(bot._analysis_window(_bars(300, freq="1min"), "1m", [])) == 300


# ── стартовая загрузка истории ──

class DailyLoader:
    def __init__(self, first):
        self.first = pd.Timestamp(first)
        self.starts = []

    def __call__(self, *, ticker, instrument_type, timeframe, start_date=None, end_date=None, **_):
        self.starts.append(start_date)
        # Без end_date загрузчик истории сам режет срез рыночным моментом — началом прогона.
        end = pd.Timestamp(end_date or "2026-10-12")
        start = end - timedelta(days=30) if start_date is None else pd.Timestamp(start_date)
        stamps = pd.date_range(max(start, self.first).normalize(), end - timedelta(days=1), freq="1D")
        frame = pd.DataFrame({"datetime": stamps, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                              "volume": 1})
        return frame, "uid-sber"


def _history_cache(loader, depth):
    clock = HistoricalClock("2026-10-12", "2026-10-20", timedelta(minutes=1), sleeper=lambda _: None)
    timeline = MultiTimeframeScheduler(["1m"], clock=clock)
    return MarketDataCache(loader, timeline, clock=clock, history_depth=depth)


def test_history_first_load_deepens_to_analysis_depth():
    loader = DailyLoader("2024-01-01")
    frame = _history_cache(loader, 150).frame_for(SBER, "1d")
    assert len(frame) >= 150
    assert loader.starts[0] is None and len(loader.starts) == 4  # 30 дней, затем 60, 120, 240


def test_young_contract_stops_when_history_ends():
    loader = DailyLoader("2026-07-24")  # 80 дневных свечей
    frame = _history_cache(loader, 150).frame_for(SBER, "1d")
    assert len(frame) == 80
    assert len(loader.starts) == 4  # окно 240 дней не дало более ранних свечей — расширение остановлено


def test_history_without_depth_keeps_thirty_days():
    loader = DailyLoader("2024-01-01")
    assert len(_history_cache(loader, 0).frame_for(SBER, "1d")) == 30 and len(loader.starts) == 1
