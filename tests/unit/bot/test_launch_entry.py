import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import run
from src.bot.launch import (
    ALL, MODE_EMULATION, SOURCE_CLI, SOURCE_MENU, Chosen, LaunchRequest, Rejection, Resolution,
)
from src.bot.launch_control import LaunchControl, parse_command
from src.data.market_store import MarketDataStore

NG = Chosen("NG", "NG-12.26", "future", 52, "NG", True)
NG_TUPLE = ("NG (Природный газ) — NG-12.26", "NGZ6", "future", "NG-12.26")


def _no_input(*_args):
    raise AssertionError("input() не должен вызываться")


class TestMain:
    @patch("builtins.input", side_effect=_no_input)
    @patch("run._run_live", return_value=0)
    def test_parameters_start_emulation_without_questions(self, run_live, _input):
        assert run.main(["--mode", "emulation", "--instruments", "NG"]) == 0
        request = run_live.call_args.args[0]
        assert request.instruments == ("NG",) and request.source == SOURCE_CLI

    @patch("builtins.input", side_effect=_no_input)
    @patch("run._run_history", return_value=0)
    def test_history_parameters(self, run_history, _input):
        assert run.main(["--mode", "history", "--from", "2026-09-01 10:00", "--to", "2026-09-02 10:00", "--pause", "0"]) == 0
        start, end, pause, request = run_history.call_args.args
        assert (start, end, pause) == (datetime(2026, 9, 1, 10), datetime(2026, 9, 2, 10), 0.0)
        assert request.source == SOURCE_CLI

    @patch("builtins.input", side_effect=_no_input)
    @patch("run._run_live", return_value=0)
    def test_bad_parameters_exit_with_two(self, run_live, _input, capsys):
        assert run.main(["--mode", "emulation", "--wait-command", "--instruments", "NG"]) == 2
        run_live.assert_not_called()
        assert "одновременно" in capsys.readouterr().out

    @patch("run._run_live", return_value=0)
    @patch("builtins.input", side_effect=["1", "2"])
    def test_menu_can_wait_for_command(self, _input, run_live):
        assert run.main() == 0
        request = run_live.call_args.args[0]
        assert request.wait_command and request.source == SOURCE_MENU


class TestChooseInstruments:
    def _choose(self, launch, **kwargs):
        defaults = dict(history=False, client_provider=None, clock=None, early=None, control=None, export_server=None)
        defaults.update(kwargs)
        return run._choose_instruments(launch, **defaults)

    def test_wait_without_control_key_stops(self, capsys):
        launch = LaunchRequest(MODE_EMULATION, SOURCE_CLI, None, wait_command=True)
        assert self._choose(launch) is None
        assert "ROBOT_CONTROL_TOKEN" in capsys.readouterr().out

    def test_rejected_parameters_stop(self, capsys):
        launch = LaunchRequest(MODE_EMULATION, SOURCE_CLI, ("NG", "AFLT"))
        resolution = Resolution([NG_TUPLE], [NG], [Rejection("AFLT", "Неизвестный инструмент.")])
        with patch.object(run, "_resolve_request", return_value=resolution):
            assert self._choose(launch) is None
        out = capsys.readouterr().out
        assert "✗ AFLT" in out and "Торговля не запущена" in out

    def test_accepted_parameters_mark_running(self, capsys):
        launch = LaunchRequest(MODE_EMULATION, SOURCE_CLI, ("NG",))
        control = LaunchControl()
        with patch.object(run, "_resolve_request", return_value=Resolution([NG_TUPLE], [NG], [])):
            assert self._choose(launch, control=control) == [NG_TUPLE]
        assert "Повторить запуск: --mode emulation --instruments NG-12.26" in capsys.readouterr().out
        snapshot = control.snapshot()
        assert snapshot["state"] == "running" and snapshot["source"] == SOURCE_CLI

    def test_menu_drops_instruments_without_assignments(self, monkeypatch, capsys):
        monkeypatch.setattr(run, "SHARE_STRATEGIES", {"SBER": ["a"]})
        monkeypatch.setattr(run, "FUTURE_STRATEGIES", {"NG": ["a"]})
        picked = [("SBER", "SBER", "share", "SBER"), ("GAZP", "GAZP", "share", "GAZP"), NG_TUPLE]
        with patch.object(run, "select_instruments", return_value=picked):
            selected = self._choose(LaunchRequest(MODE_EMULATION, SOURCE_MENU, None))
        assert selected == [picked[0], NG_TUPLE]
        assert "✗ GAZP" in capsys.readouterr().out

    def test_direct_call_keeps_old_menu(self):
        with patch.object(run, "select_instruments", return_value=[NG_TUPLE]):
            assert self._choose(None) == [NG_TUPLE]

    def test_waits_for_command_until_accepted(self, capsys):
        control = LaunchControl(reply_timeout=5)
        export_server = SimpleNamespace(address=("127.0.0.1", 8101))
        results = iter([Resolution([], [], [Rejection("AFLT", "нет привязок")]), Resolution([NG_TUPLE], [NG], [])])
        answers = []

        def send(body):
            answers.append(control.submit(parse_command(body))[0])

        def client():
            for _ in range(500):                      # главный поток ещё не начал принимать команды
                if control._accepting:
                    break
                threading.Event().wait(0.01)
            send({"kind": "emulation", "instruments": ["AFLT"]})
            send({"kind": "emulation", "instruments": ["NG"]})

        thread = threading.Thread(target=client, daemon=True)
        with patch.object(run, "_resolve_request", side_effect=lambda *a, **k: next(results)):
            thread.start()
            selected = self._choose(LaunchRequest(MODE_EMULATION, SOURCE_CLI, None, wait_command=True),
                                    control=control, export_server=export_server)
        thread.join(5)
        assert selected == [NG_TUPLE]
        assert answers == [422, 201]
        out = capsys.readouterr().out
        assert "Робот ждёт команду запуска: POST http://127.0.0.1:8101/api/v1/contours" in out


def test_local_api_starts_with_control_key_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "MARKET_DATA_EXPORT_TOKEN", None)
    monkeypatch.setattr(run, "ROBOT_CONTROL_TOKEN", "control-test-key")
    monkeypatch.setattr(run, "MARKET_DATA_EXPORT_PORT", 0)
    try:
        server = run._start_local_api(MarketDataStore(tmp_path / "market.sqlite3"), tmp_path, control=LaunchControl())
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    try:
        assert server is not None and server.control_token == "control-test-key" and server.token is None
        out = capsys.readouterr().out
        assert "MARKET_DATA_EXPORT_TOKEN" in out and "control-test-key" not in out
    finally:
        server.close()


def test_control_key_is_a_logging_secret():
    import inspect

    assert "ROBOT_CONTROL_TOKEN" in inspect.getsource(run._launch)
