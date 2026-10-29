from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


from src.broker import ExecutionEvent, ExecutionStatus
from src.logging_setup import LogRotation
from src.history.report import (
    CalendarScheduleMetadata,
    MarketDataSync,
    REPORT_JSON,
    REPORT_TXT,
    RunMetrics,
    collect_result,
    completion_line,
    crash_line,
    write_report,
)
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

UTC = timezone.utc
START = datetime(2023, 6, 1, 10, 0)
END = START + timedelta(hours=2)
NAMES = {"NGV6": "NG-10.26"}


def _plan(trade_id="trade-1", entry="100.0") -> TradePlan:
    return TradePlan(
        trade_id, "assignment-1", "NGV6", "BUY", f"signal-{trade_id}",
        Decimal(entry), Decimal("96"),
        (TargetPlan("tp-1", Decimal("110"), Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}),
        START, "1m",
    )


class _StubBroker:
    def register_trade(self, plan) -> None:
        return None


def _fill(reducer, command_id, trade_id, quantity, price, fee="1.00", when=START):
    return reducer.apply(
        ExecutionEvent(
            execution_id=f"exec-{command_id}",
            order_id=None,
            command_id=command_id,
            trade_id=trade_id,
            status=ExecutionStatus.FILL,
            filled_quantity=quantity,
            price=Decimal(price),
            fee=Decimal(fee),
            timestamp=when,
            reason="limit-reached",
        )
    )


def _storage_with_round_trip(tmp_path) -> Storage:
    """Прогон с одной открытой и одной закрытой сделкой: P&L известен заранее."""
    storage = Storage(tmp_path / "trades.sqlite3", initial_deposit="100000")
    reducer = ExecutionReducer(storage)
    manager = TradeManager(storage, _StubBroker())

    manager.submit_plan(
        _plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1),
        price_step=Decimal("1"), step_cost=Decimal("1"),
    )
    _fill(reducer, "open-1", "trade-1", 1, "100.0", when=START)
    manager.submit_action(
        CloseTrade("close-1", "trade-1", 1, "target-reached"),
    )
    _fill(reducer, "close-1", "trade-1", 1, "110.0", when=START + timedelta(hours=1))
    storage.set_names(NAMES)
    return storage


def _metrics(**kw) -> RunMetrics:
    payload = dict(
        start=START,
        end=END,
        ticks=120,
        missed_bars=2,
        gaps=1,
        longest_gap_seconds=6600.0,
        covered=True,
        stop_reason="диапазон [2023-06-01 10:00 — 2023-06-01 12:00) обработан полностью",
        market_now=END,
    )
    payload.update(kw)
    return RunMetrics(**payload)


class TestCollectResult:
    def test_pnl_matches_journal_account(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        result = collect_result(storage)

        account = storage.connection.execute(
            "SELECT realized_pnl, fees, net_realized_pnl, balance FROM account WHERE account_id = 1"
        ).fetchone()
        assert result.realized_pnl == Decimal(account[0])
        assert result.fees == Decimal(account[1])
        assert result.net_pnl == Decimal(account[2])
        assert result.balance == Decimal(account[3])

    def test_round_trip_profit_is_ten_minus_fees(self, tmp_path):
        result = collect_result(_storage_with_round_trip(tmp_path))

        assert result.realized_pnl == Decimal("10")
        assert result.fees == Decimal("2.00")
        assert result.net_pnl == Decimal("8.00")

    def test_counts_of_trades_orders_and_fills(self, tmp_path):
        result = collect_result(_storage_with_round_trip(tmp_path))

        assert result.trades == 1
        assert result.filled_orders == 2
        assert result.fills == 2

    def test_closed_position_not_reported_as_open(self, tmp_path):
        result = collect_result(_storage_with_round_trip(tmp_path))

        assert result.open_positions == []

    def test_open_position_uses_short_contract_name(self, tmp_path):
        storage = Storage(tmp_path / "trades.sqlite3", initial_deposit="100000")
        manager = TradeManager(storage, _StubBroker())
        manager.submit_plan(
            _plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1),
            price_step=Decimal("1"), step_cost=Decimal("1"),
        )
        _fill(ExecutionReducer(storage), "open-1", "trade-1", 1, "100.0")
        storage.set_names(NAMES)

        result = collect_result(storage)

        assert result.open_positions == ["NG-10.26 1"]

    def test_empty_journal_gives_zero_result(self, tmp_path):
        with Storage(tmp_path / "trades.sqlite3") as storage:
            result = collect_result(storage)

        assert result.net_pnl == Decimal("0.00")
        assert result.trades == 0
        assert result.open_positions == []


class TestWriteReport:
    def test_calendar_schedule_is_written_to_both_reports(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        schedule = CalendarScheduleMetadata(
            source="emulator",
            snapshot_generation=42,
            snapshot_pinned=True,
            exchanges=("MOEX",),
            day_versions=(("MOEX", "2026-10-13", "version-1", "broker"),),
            missing_dates=("2026-10-14",),
            unavailable_reason="часть дат отсутствует в снимке",
            provenance_counts={"broker": 1},
            binding_provenance={"MOEX": ("broker",)},
            use_reconstructed=True,
        )

        txt, js = write_report(
            tmp_path, _metrics(calendar_schedule=schedule), collect_result(storage)
        )
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))
        text = txt.read_text(encoding="utf-8")

        assert payload["calendar_schedule"] == {
            "source": "emulator",
            "snapshot_generation": 42,
            "snapshot_pinned": True,
            "exchanges": ["MOEX"],
            "day_versions": [
                {"exchange": "MOEX", "date": "2026-10-13", "version": "version-1", "provenance": "broker"}
            ],
            "missing_dates": ["2026-10-14"],
            "unavailable_reason": "часть дат отсутствует в снимке",
            "provenance_counts": {"broker": 1},
            "rules_versions": [],
            "binding_provenance": {"MOEX": ["broker"]},
            "use_reconstructed": True,
        }
        assert "Источник: emulator" in text
        assert "Снимок: закреплён (42)" in text
        assert "Режимы торгов: MOEX" in text
        assert "MOEX 2026-10-13: version-1 (подтверждено брокером)" in text
        assert "Дни расписания: подтверждено брокером — 1" in text
        assert "Даты без расписания: 2026-10-14" in text
        assert "Причина недоступности расписания: часть дат отсутствует в снимке" in text

    def test_mixed_schedule_shows_provenance_and_date_ranges(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        versions = tuple(
            ("forts_futures_weekend", f"2025-04-{day:02d}", f"v{day}", "reconstructed") for day in range(1, 26)
        )
        schedule = CalendarScheduleMetadata(
            source="emulator",
            exchanges=("forts_futures_weekend",),
            day_versions=versions,
            missing_dates=("2025-03-10", "2025-03-11", "2025-03-12", "2025-03-14", "2025-03-17"),
            provenance_counts={"broker": 6, "reconstructed": 25},
            rules_versions=("forts_futures_weekend/1",),
            binding_provenance={"forts_futures_weekend": ("reconstructed",)},
            use_reconstructed=True,
        )

        txt, js = write_report(tmp_path, _metrics(calendar_schedule=schedule), collect_result(storage))
        text = txt.read_text(encoding="utf-8")
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))

        assert "Восстановленное расписание: используется" in text
        assert "Дни расписания: подтверждено брокером — 6, восстановлено — 25" in text
        assert "Версии правил восстановления: forts_futures_weekend/1" in text
        assert "Привязки режимов: forts_futures_weekend (восстановлено)" in text
        assert "Версии дней: 25 (полный список — в report.json)" in text
        assert "Даты без расписания: 2025-03-10 — 2025-03-12, 2025-03-14, 2025-03-17" in text
        assert len(payload["calendar_schedule"]["day_versions"]) == 25
        assert payload["calendar_schedule"]["missing_dates"][0] == "2025-03-10"

    def test_calendar_schedule_is_omitted_when_absent(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        txt, js = write_report(tmp_path, _metrics(), collect_result(storage))
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))

        assert "calendar_schedule" not in payload
        assert "Расписание торгов:" not in txt.read_text(encoding="utf-8")

    def test_complete_log_is_reported(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        rotation = LogRotation(rollovers=0, backup_count=0, retained_from=None)

        txt, js = write_report(tmp_path, _metrics(log_rotation=rotation), collect_result(storage))
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))

        assert "Лог прогона: полный (ротаций: 0)" in txt.read_text(encoding="utf-8")
        assert payload["log"] == {"complete": True, "rollovers": 0, "backup_count": 0, "retained_from": None}

    def test_truncated_log_warns_with_market_moment(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        rotation = LogRotation(rollovers=9, backup_count=5, retained_from=datetime(2023, 6, 1, 11, 30))

        txt, js = write_report(tmp_path, _metrics(log_rotation=rotation), collect_result(storage))
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))
        text = txt.read_text(encoding="utf-8")

        assert "Лог прогона: НЕПОЛНЫЙ" in text
        assert "9 ротаций при backup_count=5" in text
        assert "сохранён с рыночного момента 2023-06-01 11:30" in text
        assert payload["log"] == {"complete": False, "rollovers": 9, "backup_count": 5,
                                  "retained_from": "2023-06-01 11:30"}

    def test_log_block_is_omitted_without_logging(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        txt, js = write_report(tmp_path, _metrics(), collect_result(storage))

        assert "Лог прогона" not in txt.read_text(encoding="utf-8")
        assert __import__("json").loads(js.read_text(encoding="utf-8"))["log"] is None

    def test_sync_snapshot_is_written_to_both_reports(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        sync = MarketDataSync(
            status="synchronized", producer_id="robot-db-1",
            target_change_id=12, after_id=12, snapshot_generation=42,
        )

        txt, js = write_report(
            tmp_path, _metrics(market_data_sync=sync), collect_result(storage)
        )
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))

        assert payload["market_data_sync"]["status"] == "synchronized"
        assert payload["market_data_sync"]["snapshot_generation"] == 42
        assert payload["market_data_sync"]["after_id"] == 12
        assert "Импорт My Robot: подтверждён" in txt.read_text(encoding="utf-8")
        assert "robot-db-1 / 12/12 / 42" in txt.read_text(encoding="utf-8")

    def test_unconfirmed_sync_is_preserved_in_crash_report(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        sync = MarketDataSync(reason="My Robot недоступен")

        txt, js = write_report(
            tmp_path, _metrics(crashed=True, market_data_sync=sync), collect_result(storage)
        )
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))

        assert payload["market_data_sync"] == {
            "status": "unconfirmed", "producer_id": None,
            "target_change_id": None, "after_id": None,
            "snapshot_generation": None, "reason": "My Robot недоступен",
        }
        assert "Причина неподтверждения: My Robot недоступен" in txt.read_text(
            encoding="utf-8"
        )

    def test_writes_both_files(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        txt, js = write_report(tmp_path, _metrics(), collect_result(storage))

        assert txt == tmp_path / REPORT_TXT
        assert js == tmp_path / REPORT_JSON
        assert txt.exists() and js.exists()

    def test_text_report_is_human_readable(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        txt, _ = write_report(tmp_path, _metrics(), collect_result(storage))
        text = txt.read_text(encoding="utf-8")

        assert "Исторический прогон торгового робота" in text
        assert "Диапазон:        2023-06-01 10:00 — 2023-06-01 12:00" in text
        assert "Обработано тиков: 120" in text
        assert "Пропущено баров: 2" in text
        assert "Разрывов данных: 1 (самый длинный 1 ч 50 м)" in text
        assert "Охват диапазона: диапазон 2023-06-01 10:00 — 2023-06-01 12:00 обработан полностью" in text
        assert "Итог (P&L - комиссии): 8.00" in text
        assert "Комиссии:         2.00" in text
        assert "Реализованный P&L: 10.00" in text

    def test_json_report_has_all_fields(self, tmp_path):
        import json

        storage = _storage_with_round_trip(tmp_path)

        _, js = write_report(tmp_path, _metrics(), collect_result(storage))
        payload = json.loads(js.read_text(encoding="utf-8"))

        assert payload["range"] == {"start": "2023-06-01 10:00", "end": "2023-06-01 12:00"}
        assert payload["ticks"] == 120
        assert payload["missed_bars"] == 2
        assert payload["gaps"] == 1
        assert payload["longest_gap_seconds"] == 6600.0
        assert payload["covered"] is True
        assert payload["crashed"] is False
        assert payload["stop_reason"].startswith("диапазон")
        assert payload["result"]["net_pnl"] == "8.00"
        assert payload["result"]["fills"] == 2
        assert payload["journal"].endswith("trades.sqlite3")

    def test_crash_report_marks_market_moment(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        metrics = _metrics(
            ticks=57, crashed=True, stop_reason="KeyboardInterrupt", market_now=START
        )

        txt, js = write_report(tmp_path, metrics, collect_result(storage))
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))

        assert payload["crashed"] is True
        assert payload["market_now"] == "2023-06-01 10:00"
        assert "Рыночный момент: 2023-06-01 10:00" in txt.read_text(encoding="utf-8")

    def test_incomplete_coverage_is_stated_with_shortfall(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        metrics = _metrics(
            ticks=57,
            covered=False,
            stop_reason=(
                "данные закончились на 2023-06-01 11:00, "
                "до конца диапазона (2023-06-01 12:00) прогон не дошёл"
            ),
            market_now=START + timedelta(hours=1),
        )

        txt, js = write_report(tmp_path, metrics, collect_result(storage))
        text = txt.read_text(encoding="utf-8")

        assert "покрыт 2023-06-01 10:00 — 2023-06-01 11:00" in text
        assert "до конца диапазона (2023-06-01 12:00) не дойдено" in text
        assert __import__("json").loads(js.read_text(encoding="utf-8"))["covered"] is False

    def test_checked_horizon_block_when_search_was_limited(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)
        metrics = _metrics(
            covered=False,
            ticks=15,
            stop_reason=(
                "следующий бар не найден в проверенном горизонте "
                "[2023-06-01 10:15 — 2023-06-01 10:22]"
            ),
            market_now=START + timedelta(minutes=15),
            horizon_start=START + timedelta(minutes=15),
            horizon_end=START + timedelta(minutes=22),
            horizon_limited=True,
        )

        txt, js = write_report(tmp_path, metrics, collect_result(storage))
        text = txt.read_text(encoding="utf-8")
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))

        assert "Проверенный горизонт: 2023-06-01 10:15 — 2023-06-01 10:22" in text
        assert "ограничен семью сутками" in text
        assert payload["checked_horizon"] == {
            "start": "2023-06-01 10:15",
            "end": "2023-06-01 10:22",
            "limited": True,
        }

    def test_no_horizon_block_when_range_is_covered(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        txt, js = write_report(tmp_path, _metrics(), collect_result(storage))
        text = txt.read_text(encoding="utf-8")
        payload = __import__("json").loads(js.read_text(encoding="utf-8"))

        assert "Проверенный горизонт" not in text
        assert payload["checked_horizon"] is None

    def test_gap_line_without_gaps(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        txt, _ = write_report(
            tmp_path, _metrics(gaps=0, longest_gap_seconds=0.0), collect_result(storage)
        )

        assert "Разрывов данных: 0 (самый длинный 0 м)" in txt.read_text(encoding="utf-8")

    def test_longest_gap_formatted_in_hours_and_minutes(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        txt, _ = write_report(
            tmp_path, _metrics(longest_gap_seconds=7860.0), collect_result(storage)
        )

        assert "самый длинный 2 ч 11 м" in txt.read_text(encoding="utf-8")

    def test_open_positions_listed_in_text(self, tmp_path):
        storage = Storage(tmp_path / "trades.sqlite3", initial_deposit="100000")
        manager = TradeManager(storage, _StubBroker())
        manager.submit_plan(
            _plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1),
            price_step=Decimal("1"), step_cost=Decimal("1"),
        )
        _fill(ExecutionReducer(storage), "open-1", "trade-1", 1, "100.0")
        storage.set_names(NAMES)

        txt, _ = write_report(tmp_path, _metrics(), collect_result(storage))
        text = txt.read_text(encoding="utf-8")

        assert "Открытые позиции на момент остановки:" in text
        assert "NG-10.26 1" in text

    def test_report_dir_created(self, tmp_path):
        state_dir = tmp_path / "HIST" / "run-1"

        with Storage(tmp_path / "trades.sqlite3") as storage:
            txt, _ = write_report(state_dir, _metrics(), collect_result(storage))

        assert Path(txt).parent == state_dir
        assert txt.exists()

    def test_write_is_idempotent(self, tmp_path):
        storage = _storage_with_round_trip(tmp_path)

        first, _ = write_report(tmp_path, _metrics(), collect_result(storage))
        second, _ = write_report(tmp_path, _metrics(), collect_result(storage))

        assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


class TestConsoleLines:
    def test_completion_line_points_to_report(self, tmp_path):
        line = completion_line(tmp_path / REPORT_TXT)

        assert line == f"Прогон завершён. Отчёт: {tmp_path / REPORT_TXT}"
        assert "\n" not in line

    def test_crash_line_has_market_moment(self):
        line = crash_line(_metrics(crashed=True, market_now=datetime(2023, 6, 1, 14, 35, tzinfo=UTC)))

        assert line == "Прогон аварийно остановлен на 2023-06-01 14:35"
        assert "\n" not in line


def test_segment_line_explains_cutoff_shift_and_unknown_schedule():
    from src.history.report import _cutoff_note
    shifted = {"cutoff_source": "session-end", "calendar_cutoff": "2025-06-29T16:05:00Z",
               "cutoff": "2025-06-27T20:30:00Z", "session_end": "2025-06-27T20:50:00Z"}
    assert _cutoff_note(shifted) == ("; рубеж 2025-06-29T16:05:00Z в перерыве — сдвинут на 2025-06-27T20:30:00Z"
                                     " (конец сессии 2025-06-27T20:50:00Z)")
    assert _cutoff_note({"cutoff_source": "schedule-unknown"}) == "; расписание у рубежа неизвестно — рубеж календарный"
    assert _cutoff_note({"cutoff_source": "calendar"}) == ""
    assert _cutoff_note({}) == ""
