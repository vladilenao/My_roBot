"""Циклы робота сопровождают сделку на таймфрейме её плана (management-plan-timeframe)."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
import tempfile

from src.bot import TradingBot
from src.bot.live_analysis import LiveAnalysis
from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.events import ALL_EVENT_TYPES, EventBus, EventType
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler
from src.strategies.contracts import Assignment

from tests.unit.bot.test_trading_bot import (
    FakeCache, FakeTimeline, RecordingChannel, _df, _inst, _make_bot, _make_strategy,
)
from tests.unit.test_live_analysis import LEGACY_QUEUE, Strategy, frame


def _trade(timeframe, **parameters):
    return SimpleNamespace(plan=SimpleNamespace(timeframe=timeframe, profile=SimpleNamespace(parameters=parameters)))


def _live(manager, *, now=datetime(2026, 10, 12, 10, 1)):
    """SBER с единственной привязкой на 15m; ТФ сделки задаёт менеджер."""
    clock = [now]
    timeline = MultiTimeframeScheduler(["1h", "15m", "1m"], clock=lambda: clock[0])
    data = {("SBER", tf): frame("2026-10-12 06:00", tf) for tf in ("1h", "15m", "1m")}
    store = MarketDataStore(Path(tempfile.mkdtemp()) / "market.sqlite3")
    cache = MarketDataCache(lambda **kw: (data[kw["ticker"], kw["timeframe"]], kw["ticker"]), timeline,
                            request_budget=RequestBudget(lambda: clock[0], 0), market_store=store,
                            live_cutover=datetime(2026, 10, 12, 6, 0))
    cache.contour_id = "emulation"
    worker = IngestionWorker(cache, settings=LEGACY_QUEUE)
    events = []
    bus = EventBus()
    bus.subscribe(SimpleNamespace(supported_types=ALL_EVENT_TYPES, handle=events.append))
    bindings = {"SBER": [Assignment(id="SBER-15m", strategy="macd_rsi_stoch", management="levels_rr",
                                    filter_profile="raw", timeframe="15m")]}
    bot = TradingBot([Instrument("SBER", "SBER", "share", "SBER", "SBER")], bus, {"macd_rsi_stoch": object()},
                     cache, timeline, strategy_factory=lambda *args, **kw: Strategy(), share_strategies=bindings,
                     trade_manager=manager, post_tick=Mock(return_value=True), heartbeat_every_ticks=2,
                     live_data_waiter=worker.run_until_idle)
    bot._validate()
    live = LiveAnalysis(bot)
    live.worker = worker
    return live, data, events, clock


def _manage_calls(manager, timeframe):
    return [call for call in manager.manage.call_args_list if call.kwargs.get("timeframe") == timeframe]


def test_management_needs_are_plan_timeframes_with_profile_warmup():
    manager = Mock()
    manager.management_obligations.return_value = (
        _trade("1h", ma_slow_period=40), _trade(""), _trade("bogus"), _trade("15m"),
    )
    bot = TradingBot([], EventBus(), {}, Mock(), MultiTimeframeScheduler(["1h"]), trade_manager=manager)

    needs = bot._management_needs(Instrument("SBER", "SBER", "share", "SBER", "SBER"))

    assert needs == {"1h": 41, "15m": 15}


def test_trade_timeframe_without_binding_is_managed_on_its_own_bar_close():
    manager = Mock()
    manager.management_obligations.return_value = (_trade("1h", ma_slow_period=40),)
    manager.manage.return_value = ()
    manager.actions_for_signal.return_value = SimpleNamespace(actions=(), rejections=())
    live, data, events, clock = _live(manager)
    live.bootstrap()
    # Обзор строится только для привязок: ТФ сделки без привязок его не получает.
    assert {e.timeframe for e in events if e.type is EventType.STARTUP_ANALYSIS} == {"15m"}
    assert ("SBER", "1h") in live.states

    data["SBER", "1h"] = frame("2026-10-12 09:00")
    data["SBER", "15m"] = frame("2026-10-12 09:45", "15m")
    data["SBER", "1m"] = frame("2026-10-12 09:59", "1m", count=300)
    clock[0] += timedelta(seconds=5)
    live.worker.run_until_idle()
    live.step()

    (hourly,) = _manage_calls(manager, "1h")
    instrument, assignments, managed_frame, context = hourly.args
    assert assignments == [] and context is None
    assert len(managed_frame) == 41
    assert str(managed_frame["datetime"].iloc[-1]) == "2026-10-12 09:00:00"

    # Тот же час повторно не сопровождается.
    live.step()
    assert len(_manage_calls(manager, "1h")) == 1


def test_historical_cycle_manages_trade_timeframe_without_binding_and_keeps_1m():
    trade_manager = MagicMock()
    trade_manager.manage.return_value = ()
    trade_manager.management_obligations.return_value = (_trade("1h", ma_slow_period=40),)
    bot = _make_bot(
        timeline=FakeTimeline(timeframes=("1m", "1h")),
        cache=FakeCache(frames={("SBER", "1m"): _df(), ("SBER", "1h"): _df()}),
        bus=RecordingChannel().bus, strategy=_make_strategy(),
        share={}, trade_manager=trade_manager,
    )
    bot._instruments = [_inst("SBER", "SBER", "share")]

    bot.run()

    # 1m — для таймаутов и экспирации; профиль сделки менеджер считает только на 1h.
    assert [call.kwargs["timeframe"] for call in trade_manager.manage.call_args_list] == ["1m", "1h"]
    assert all(call.args[1] == [] for call in trade_manager.manage.call_args_list)
