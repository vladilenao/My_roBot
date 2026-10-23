"""Служебный вход процесса контура ``--contour emulation`` (add-contour-process, D1, D2)."""

import io
import json
from types import SimpleNamespace

import pytest

import run
from src.assembly.process import contour_dir, write_launch
from src.bot.run_session import StoppableRunSession
from src.runtime_lock import acquire_runtime_lock


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(run, "setup_logging", lambda **kw: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    handlers = []
    monkeypatch.setattr("signal.signal", lambda sig, handler: handlers.append(handler))
    return tmp_path


def _launch(directory, **extra):
    write_launch(directory, {
        "contour_id": "emulation", "kind": "emulation", "telegram_channel_env": "TELEGRAM_CHANNEL_ID",
        "instruments": [{"label": "SBER", "ticker": "SBER", "instrument_type": "share", "short_name": "SBER",
                         "uid": "uid-sber", "family": None}],
        "contracts": {"uid-sber": {"ticker": "uid-sber", "price_step": 0.01, "step_cost": 0.1, "go_buy": 0.0,
                                   "go_sell": 0.0, "currency": "RUB", "expiration_date": None}},
        **extra,
    })


def test_contour_runs_on_feed_with_launch_file(data_dir, monkeypatch):
    seen = {}

    class FakeServices:
        def __init__(self, store, *, contour_id, data_dir, tracks_obligations, poll_seconds):
            seen.update(contour_id=contour_id, data_dir=data_dir, store=store.path.name)

        def serve(self, demand):
            return "waiter"

        def close(self):
            seen["closed"] = True

    class FakeContour:
        def __init__(self, settings, services):
            seen["settings"] = settings

        def build(self):
            return self

        def demand(self):
            return None

        def run(self, waiter):
            seen["waiter"] = waiter

        def close(self):
            seen["contour_closed"] = True

    monkeypatch.setattr("src.assembly.feed.FeedMarketServices", FakeServices)
    monkeypatch.setattr(run, "TradingContour", FakeContour)
    directory = contour_dir(data_dir, "emulation")
    _launch(directory)

    assert run.main(["--contour", "emulation"]) == 0

    settings = seen["settings"]
    assert settings.state_dir == directory and settings.client_provider is None
    assert [item.ticker for item in settings.instruments] == ["SBER"]
    assert settings.contracts["uid-sber"].price_step == 0.01
    assert isinstance(settings.session, StoppableRunSession)
    assert seen["contour_id"] == "emulation" and seen["data_dir"] == data_dir and seen["store"] == "market-data.sqlite3"
    assert seen["waiter"] == "waiter" and seen["closed"] and seen["contour_closed"]


def test_missing_launch_file_stops_before_trading(data_dir, capsys):
    assert run.main(["--contour", "emulation"]) == 2
    assert "Нет файла запуска" in capsys.readouterr().out


def test_incompatible_launch_file(data_dir, capsys):
    directory = contour_dir(data_dir, "emulation")
    directory.mkdir(parents=True)
    (directory / "launch.json").write_text(json.dumps({"schema_version": 2, "contour_id": "emulation"}))

    assert run.main(["--contour", "emulation"]) == 2
    assert "разных версий" in capsys.readouterr().out


def test_second_contour_process_does_not_take_lock(data_dir):
    directory = contour_dir(data_dir, "emulation")
    directory.mkdir(parents=True)
    acquire_runtime_lock(directory)  # блокировку держит живой процесс (этот тест)

    assert run.main(["--contour", "emulation"]) == 1


def test_unknown_contour_kind(data_dir, capsys):
    assert run.main(["--contour", "account"]) == 2
    assert "Неизвестный вид контура" in capsys.readouterr().out


def test_stoppable_session():
    event = SimpleNamespace(is_set=lambda: True)
    assert StoppableRunSession(event).should_stop()


def test_real_contour_process_starts_on_empty_feed_and_stops_on_command(data_dir, monkeypatch, capsys):
    """Настоящая сборка контура на пустой рыночной базе: без брокера, без арбитра, остановка по stop."""
    import os
    import threading
    import time

    from src.assembly import contour as contour_module
    from src.assembly import feed
    from src.data.market_store import MarketDataStore
    from src.notifier import factory

    monkeypatch.setattr(contour_module, "build_channels",
                        lambda names=None, state_dir=None: factory.build_channels(["console"], state_dir=state_dir))
    monkeypatch.setattr(feed, "MARKET_DATA_SETTINGS", SimpleNamespace(**{
        **vars(feed.MARKET_DATA_SETTINGS), "startup_wait_timeout_secs": 0.5}))
    MarketDataStore(data_dir / "market-data.sqlite3")
    _launch(contour_dir(data_dir, "emulation"))
    read, write = os.pipe()
    monkeypatch.setattr("sys.stdin", os.fdopen(read))
    writer = os.fdopen(write, "w")

    def stop_later():
        time.sleep(1.5)
        writer.write("stop\n")
        writer.flush()

    threading.Thread(target=stop_later, daemon=True).start()

    assert run.main(["--contour", "emulation"]) == 0
    demand = MarketDataStore(data_dir / "market-data.sqlite3").contour_demands()
    assert {row["instrument"]["ticker"] for row in demand} == {"SBER"}
    assert (contour_dir(data_dir, "emulation") / "trades.sqlite3").exists()
    assert "ПРОГРЕВ" in capsys.readouterr().out
