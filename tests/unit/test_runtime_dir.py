from datetime import datetime
from pathlib import Path

from src.config import DATA_DIR, HIST_DIR, app_dir, run_dir_name, runtime_dir
from src.trade_journal.storage import Storage


class TestRuntimeDir:
    def test_default_path_unchanged(self):
        assert runtime_dir() == app_dir() / DATA_DIR

    def test_default_path_is_absolute(self):
        assert runtime_dir().is_absolute()

    def test_run_base_used_as_is(self, tmp_path):
        assert runtime_dir(tmp_path) == tmp_path

    def test_relative_run_base_resolved_against_app_dir(self):
        base = Path(HIST_DIR) / "20230101T000000-20230101T010000-20230101T020000"

        assert runtime_dir(base) == app_dir() / base


class TestRunDirName:
    def test_contains_boundaries_and_start_time(self):
        name = run_dir_name(
            datetime(2023, 1, 1, 0, 0), datetime(2023, 1, 2, 0, 0), datetime(2026, 9, 27, 12, 0, 0)
        )

        assert name == "20260927T120000-20230101T000000-20230102T000000"

    def test_name_is_filesystem_safe(self):
        name = run_dir_name(
            datetime(2023, 1, 1), datetime(2023, 1, 2), datetime(2026, 9, 27, 12, 0, 0)
        )

        assert all(char.isalnum() or char in "-_" for char in name)

    def test_different_runs_get_different_names(self):
        start, end = datetime(2023, 1, 1), datetime(2023, 1, 2)
        first = run_dir_name(start, end, datetime(2026, 9, 27, 12, 0, 0))
        second = run_dir_name(start, end, datetime(2026, 9, 27, 12, 0, 1))

        assert first != second


class TestStateDirIsolation:
    def test_journal_files_land_in_run_dir_only(self, tmp_path):
        run_base = tmp_path / "HIST" / run_dir_name(
            datetime(2023, 1, 1), datetime(2023, 1, 2), datetime(2026, 9, 27, 12, 0, 0)
        )
        state_dir = runtime_dir(run_base)
        state_dir.mkdir(parents=True)

        with Storage(
            state_dir / "trades.sqlite3",
            journal_path=state_dir / "trade_event.csv",
            positions_path=state_dir / "trade_summary.csv",
        ) as storage:
            tables = {
                row[0] for row in storage.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        assert "trades" in tables
        assert (state_dir / "trades.sqlite3").exists()
        assert not (Path(DATA_DIR) / "HIST").exists()
        assert run_base != runtime_dir()
