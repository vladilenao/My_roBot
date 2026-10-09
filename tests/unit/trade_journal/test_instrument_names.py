from src.trade_journal.storage import Storage


def test_set_names_persists_mapping_for_tools_without_a_running_bot(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        storage.set_names({"NGV6": "NG-10.26", "BRV6": "BR-10.26"})

        assert storage.instrument_names() == {"NGV6": "NG-10.26", "BRV6": "BR-10.26"}


def test_repeated_set_names_does_not_duplicate_rows(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        storage.set_names({"NGV6": "NG-10.26", "BRV6": "BR-10.26"})
        storage.set_names({"NGV6": "NG-10.26", "BRV6": "BR-10.26"})

        rows = storage.connection.execute(
            "SELECT count(*) FROM instrument_names"
        ).fetchone()[0]
        assert rows == 2
        assert storage.instrument_names() == {"NGV6": "NG-10.26", "BRV6": "BR-10.26"}


def test_contract_rollover_updates_existing_row_in_place(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        storage.set_names({"NGV6": "NG-10.26"})
        storage.set_names({"NGV6": "NG-12.26"})

        assert storage.instrument_names() == {"NGV6": "NG-12.26"}


def test_mapping_is_persisted_even_without_csv_exporters(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        assert storage._exporter is None

        storage.set_names({"NGV6": "NG-10.26"})

        assert storage.instrument_names() == {"NGV6": "NG-10.26"}


def test_empty_entries_are_not_persisted(tmp_path):
    with Storage(tmp_path / "trades.sqlite3") as storage:
        storage.set_names({"NGV6": "", "BRV6": "BR-10.26"})

        assert storage.instrument_names() == {"BRV6": "BR-10.26"}


def test_set_names_does_not_change_csv_projection_format(tmp_path):
    journal = tmp_path / "trade_event.csv"
    summary = tmp_path / "trade_summary.csv"
    with Storage(
        tmp_path / "trades.sqlite3",
        journal_path=journal,
        positions_path=summary,
    ) as storage:
        before = storage.connection.execute(
            "SELECT required_revision, exported_revision FROM export_state"
        ).fetchone()

        storage.set_names({"NGV6": "NG-10.26"})

        after = storage.connection.execute(
            "SELECT required_revision, exported_revision FROM export_state"
        ).fetchone()
        assert after == before
