import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import run
from src.assembly import core
from src.bot.launch import Chosen, Rejection, Resolution
from src.bot.launch_control import LaunchControl, parse_command

NG = Chosen("NG", "NG-12.26", "future", 52, "NG", True)
NG_TUPLE = ("NG (Природный газ) — NG-12.26", "NGZ6", "future", "NG-12.26")


def _no_input(*_args):
    raise AssertionError("input() не должен вызываться")


class TestMain:
    @patch("builtins.input", side_effect=_no_input)
    @patch("run._run_robot", return_value=0)
    def test_start_without_parameters_and_questions(self, run_robot, _input):
        assert run.main([]) == 0
        run_robot.assert_called_once_with()

    @pytest.mark.parametrize("argv", [
        ["--mode", "emulation", "--instruments", "NG"],
        ["--mode", "history", "--from", "2026-09-01 10:00", "--to", "2026-09-02 10:00", "--pause", "0"],
        ["--no-prompt"],
        ["--wait-command"],
    ])
    @patch("builtins.input", side_effect=_no_input)
    @patch("run._run_robot", return_value=0)
    def test_old_parameters_exit_with_two_and_api_hint(self, run_robot, _input, argv, capsys):
        assert run.main(argv) == 2
        run_robot.assert_not_called()
        out = capsys.readouterr().out
        assert "POST /api/v1/contours" in out and '"kind": "history"' in out

    @patch("run._run_history_contour", return_value=0)
    def test_history_contour_entry(self, run_history_contour):
        assert run.main(["--contour", "history", "20261024-101500"]) == 0
        run_history_contour.assert_called_once_with("20261024-101500")

    def test_unknown_contour_kind(self, capsys):
        assert run.main(["--contour", "account"]) == 2
        assert "emulation и history" in capsys.readouterr().out


def test_waits_for_command_until_accepted(capsys):
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
        selected = run._await_command(control, export_server, None, None, None)
    thread.join(5)
    assert selected == [NG_TUPLE]
    assert answers == [422, 201]
    out = capsys.readouterr().out
    assert "Робот ждёт команду запуска: POST http://127.0.0.1:8101/api/v1/contours" in out
    assert 'Повторить запуск: POST /api/v1/contours {"kind": "emulation", "instruments": ["NG-12.26"]}' in out


def test_control_api_starts_before_market_data_with_control_key_only(monkeypatch, capsys):
    monkeypatch.setattr(core, "MARKET_DATA_EXPORT_TOKEN", None)
    monkeypatch.setattr(core, "ROBOT_CONTROL_TOKEN", "control-test-key")
    monkeypatch.setattr(core, "MARKET_DATA_EXPORT_PORT", 0)
    try:
        server = core._start_control_api(LaunchControl())
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    try:
        # Сравнение без вывода значения ключа: при сбое pytest не напечатает секрет.
        key_matches = server.control_token == "control-test-key"
        assert key_matches
        assert server.token is None and server.store is None
        out = capsys.readouterr().out
        assert "MARKET_DATA_EXPORT_TOKEN" in out and "control-test-key" not in out
    finally:
        server.close()


def test_control_key_is_a_logging_secret():
    import inspect

    source = inspect.getsource(run)
    assert "ROBOT_CONTROL_TOKEN, MARKET_STRUCTURE_REDIS_PASSWORD)" in source
    assert source.count("secrets=_SECRETS") >= 3
