"""Roster live-ingestion и режимы запуска: UID, lifecycle, alias, history/NotifyOnly."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import run
from src.assembly import contour, core
from src.bot import TradingBot
from src.bot.live_analysis import LiveAnalysis
from src.data.cache import MarketDataCache
from src.data.ingestion import IngestionWorker
from src.data.ingestion_settings import MarketDataSettings
from src.data.market_store import MarketDataStore
from src.data.request_budget import RequestBudget
from src.events import EventBus
from src.instruments import Instrument
from src.scheduler.timing import MultiTimeframeScheduler
from src.strategies.contracts import Assignment, Decision, SignalType

NOW = datetime(2026, 10, 12, 12, 5)


def live_cache(tmp_path, loader=None):
    timeline = MultiTimeframeScheduler(["1m", "15m"], clock=lambda: NOW)
    return MarketDataCache(loader or (lambda **kw: (pd.DataFrame(), kw.get("instrument_id"))), timeline,
                           request_budget=RequestBudget(lambda: NOW, 0),
                           market_store=MarketDataStore(tmp_path / "market.sqlite3"))


class Strategy:
    def required_history(self):
        return 40

    def compute(self, values):
        return values

    def decide(self, values, timeframe=""):
        return Decision(SignalType.HOLD, float(values["close"].iloc[-1]))


def test_needs_are_deduplicated_per_pair_and_base_1m_is_always_present(tmp_path):
    cache = live_cache(tmp_path)
    item = Instrument("SBER", "SBER", "share", "SBER", "u")
    bindings = [Assignment(id=f"b{index}", strategy="macd_rsi_stoch", management="levels_rr",
                           filter_profile="raw", timeframe="15m") for index in range(3)]
    bot = TradingBot([item], EventBus(), {"macd_rsi_stoch": object()}, cache, cache._timeline,
                     strategy_factory=lambda *a, **k: Strategy(), share_strategies={"SBER": bindings})
    bot._validate()
    LiveAnalysis(bot).register_needs()
    needs = {key[2]: count for key, _, count in cache.live_needs()}
    assert set(needs) == {"1m", "15m"}
    assert len(cache.live_needs()) == 2  # три привязки одного ТФ — одна пара UID/ТФ


def test_draining_obligation_keeps_old_uid_and_new_warmup_does_not_move_execution(tmp_path, monkeypatch):
    cache = live_cache(tmp_path)
    old = Instrument("NG", "NGU6", "future", "NG-9.26", "old")
    new = Instrument("NG", "NGV6", "future", "NG-10.26", "new")
    lifecycle = SimpleNamespace(execution_instruments=lambda: [old, new], market_instruments=lambda: [new])
    plan = SimpleNamespace(instrument_id="old", timeframe="15m", profile=SimpleNamespace(parameters={}))
    storage = SimpleNamespace(load_trades=lambda: (SimpleNamespace(plan=plan),),
        execution_bar_boundaries=lambda: {"old": (datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc), False)})
    monkeypatch.setattr(core.LiveMarketDataPoller, "start", lambda self: None)
    runtime = SimpleNamespace(storage=storage, contract_lifecycle=lifecycle)
    core._start_live_ingestion(cache, contour.build_demand(runtime, [new], cache), None)
    needs = {(key[0], key[2]) for key, _, _ in cache.live_needs()}
    assert needs == {("old", "15m"), ("old", "1m")}  # старая 1m остаётся до terminal obligations
    assert set(cache._pending_execution_lower) == {"old"}  # новый UID не получает исполнения


def test_ambiguous_alias_is_held_without_hot_retry_and_same_ticker_uids_do_not_mix(tmp_path):
    calls = []
    def loader(**kwargs):
        calls.append(kwargs["instrument_id"])
        frame = pd.DataFrame({"datetime": [pd.Timestamp("2026-10-12 12:00")], "open": 1.0, "high": 1.0,
                              "low": 1.0, "close": 1.0 if kwargs["instrument_id"] == "A" else 2.0,
                              "volume": 1, "is_complete": True})
        return frame, kwargs["instrument_id"]
    cache = live_cache(tmp_path, loader)
    first, second = (Instrument("SI", "SIZ6", "future", "Si-12.26", uid) for uid in ("A", "B"))
    for item in (first, second):
        cache.prepare_live_history(item, "1m", 1)
    worker = IngestionWorker(cache, settings=MarketDataSettings())
    worker.run_until_idle()
    assert cache.frame_for(first, "1m")["close"].iloc[-1] == 1.0
    assert cache.frame_for(second, "1m")["close"].iloc[-1] == 2.0  # одинаковый ticker, разные UID
    legacy = Instrument("SI", "SIZ6", "future", "Si-12.26")
    cache.register_live_pair(legacy, "A", "1m")
    with pytest.raises(ValueError, match="Неоднозначный"):
        cache.register_live_pair(legacy, "B", "1m")
    # Каталог разрешил тот же финансовый alias в другой UID: пара удерживается.
    cache._uids.pop(cache._key(legacy, "1m"))
    cache.request_live_history(legacy, "1m", 1)
    worker._resolve_uid = lambda instrument: "B"
    assert worker.run_until_idle(limit=50)  # без горячего цикла
    assert cache.live_needs() == []
    assert "Неоднозначный" in cache._live_errors[cache._key(legacy, "1m")]
    assert cache.frame_for(first, "1m")["close"].iloc[-1] == 1.0  # данные A не смешаны с B


def test_history_launch_never_starts_live_ingestion(tmp_path):
    from src.history.preflight import PreflightReport
    start, end = datetime(2024, 1, 1), datetime(2024, 1, 5)
    report = PreflightReport(problems=(), notes=(), start=start, shift_to=None, checked_pairs=1, detail=[])
    with (
        patch.object(run, "setup_logging"),
        patch.object(run, "active_pairs", return_value=[("SBER", "1m")]),
        patch.object(run, "_warmup_bars", return_value=41),
        patch.object(run, "run_preflight", return_value=report),
        patch.object(contour, "build_channels", return_value=[]),
        patch.object(contour, "_build_runtime", return_value=contour._Runtime()),
        patch.object(contour, "_run_bot") as bot,
        patch.object(contour, "_finish_history"),
        patch.object(core, "_start_live_ingestion", side_effect=AssertionError("live-ingestion в истории")),
    ):
        code, _, _ = run._launch(state_dir=tmp_path / "state", client_provider=MagicMock(), clock=MagicMock(),
                                 session=MagicMock(start=start, end=end), channel_names=[], history=True,
                                 instruments=[("Сбербанк", "SBER", "share")])
    assert code == 0
    assert bot.call_args.kwargs["live_waiter"] is None


def test_notify_only_live_uses_same_ingestion_and_stream_without_factory_falls_back(tmp_path, monkeypatch, caplog):
    cache = live_cache(tmp_path)
    started = []
    monkeypatch.setattr(core.LiveMarketDataPoller, "start", lambda self: started.append(self))
    monkeypatch.setattr(core, "MARKET_DATA_SETTINGS", MarketDataSettings(live_transport="stream"))
    demand = contour.build_demand(contour._Runtime(), [Instrument("SBER", "SBER", "share", "SBER", "u")], cache)
    poller, _ = core._start_live_ingestion(cache, demand, SimpleNamespace(client_context=lambda token=None: None))
    assert started == [poller]
    assert poller._stream is None and poller._worker is not None
    assert "используется polling" in caplog.text
