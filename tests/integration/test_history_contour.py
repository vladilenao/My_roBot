"""Процесс прогона истории ``--contour history <имя>`` целиком (add-history-contour)."""

import contextlib
import json
import signal
from unittest.mock import patch

import run
from src.assembly import contour
from src.assembly.contour_control import STATUS_FILE, read_status
from src.assembly.core_state import CoreStateStore
from src.assembly.history_runs import prepare_import, run_dir
from src.assembly.process import write_launch
from src.history.report import REPORT_JSON
from src.portfolio.models import ContractMeta
from src.trade_management.intraday_settings import IntradaySettings
from tests.integration.test_historical_run import (
    END, INSTRUMENT, START, STEP, TICKER, StubEmulatorClient, StubProvider, _assignment_patch, _series,
)


class PinnedProvider(StubProvider):
    def use_snapshot(self, generation):
        self.pinned = generation


def _process(tmp_path, *, stop=False):
    run_id = "history-20261024-101500"
    store = CoreStateStore(tmp_path / "core.sqlite3")
    store.add_history_run(run_id, "20261024-101500", {})
    if stop:
        store.update_history_run(run_id, stop_requested=True)
    directory = run_dir(tmp_path, run_id)
    client = StubEmulatorClient(_series(int((END - START) / STEP)))
    provider = PinnedProvider(client)
    sync, generation, _ = prepare_import(provider)            # сверку делает робот до старта процесса
    write_launch(directory, {"contour_id": run_id, "kind": "history", "from": f"{START:%Y-%m-%d %H:%M}",
                             "to": f"{END:%Y-%m-%d %H:%M}", "pause": 0, "instruments": ["NGV6"],
                             "market_data_sync": sync.__dict__, "snapshot_generation": generation})
    with contextlib.ExitStack() as stack:
        for item in _assignment_patch():
            stack.enter_context(item)
        stack.enter_context(patch.object(run, "runtime_dir", return_value=tmp_path))
        stack.enter_context(patch.object(run, "EmulatorClientProvider", return_value=provider))
        stack.enter_context(patch.object(run, "_history_instruments", return_value=[INSTRUMENT]))
        stack.enter_context(patch.object(run, "setup_logging"))
        stack.enter_context(patch.object(run, "CONTOURS_HEARTBEAT_SECS", 0.05))
        stack.enter_context(patch.object(signal, "signal"))
        stack.enter_context(patch.object(contour, "INTRADAY_SETTINGS", IntradaySettings(enabled=False)))
        stack.enter_context(patch.object(contour, "print_contract_metadata"))
        stack.enter_context(patch.object(contour, "_load_contracts_metadata", return_value={
            TICKER: ContractMeta(ticker=TICKER, price_step=1.0, step_cost=1.0, go_buy=0.0, go_sell=0.0)}))
        code = run.main(["--contour", "history", "20261024-101500"])
    return code, directory, client, provider


def test_run_process_writes_report_status_and_uses_robot_snapshot(tmp_path):
    code, directory, client, provider = _process(tmp_path)
    assert code == 0
    report = json.loads((directory / REPORT_JSON).read_text(encoding="utf-8"))
    assert report["covered"] and report["result"]["fills"] > 0
    assert report["market_data_sync"]["snapshot_generation"] == 42
    assert client.sync_calls == 1 and provider.pinned == 42      # процесс сам sync не вызывает
    status = read_status(directory / STATUS_FILE)
    assert (status["phase"], status["exit_code"], status["exit_reason"]) == ("finishing", 0, "прогон завершён")
    assert status["start"] == "2023-06-01T10:00:00Z" and status["market_now"] == "2023-06-01T10:10:00Z"


def test_stop_flag_stops_run_with_operator_report(tmp_path):
    code, directory, _, _ = _process(tmp_path, stop=True)
    assert code == 0
    report = json.loads((directory / REPORT_JSON).read_text(encoding="utf-8"))
    assert report["stop_reason"] == "остановлено оператором" and report["crashed"] is False
    assert read_status(directory / STATUS_FILE)["exit_reason"] == "остановлено оператором"


def test_missing_folder_is_refused(tmp_path, capsys):
    with patch.object(run, "runtime_dir", return_value=tmp_path):
        assert run.main(["--contour", "history", "nope"]) == 2
    assert "Нет папки прогона" in capsys.readouterr().out
