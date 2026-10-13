"""Интеграционные тесты исторического прогона на подставном источнике.

Источник — локальный HTTP-эмулятор рыночных данных, подменённый на клиент с
детерминированными свечами: проверяется вся сборка прогона (диалог → часы →
кэш → стратегии → исполнение → журнал → отчёт), а не отдельные её части.
"""

import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from t_tech.invest import schemas
from t_tech.invest.schemas import CandleInterval

import run
from src.data.timeutil import to_naive
from src.history.preflight import PreflightReport
from src.history.report import REPORT_JSON, REPORT_TXT
from src.portfolio.models import ContractMeta
from src.strategies.contracts import Assignment, Decision, SignalType
from src.trade_journal.storage import Storage

TICKER = "NGV6"
UID = "uid-ngv6"
NAME = "NG-10.26"
START = datetime(2023, 6, 1, 10, 0)
END = datetime(2023, 6, 1, 10, 10)
STEP = timedelta(minutes=1)
WARMUP = timedelta(minutes=60)
PROFILE = "ma_cloud"
TF = "1m"


def _series(run_bars: int, *, warmup: int = 60, base: float = 100.0):
    """Детерминированные бары: прогрев, вход на росте, затем обвал к стопу.

    Профиль ``ma_cloud`` планирует вход по скользящим средним, поэтому прогрева
    достаточно для MA40, а падение во второй части диапазона закрывает позицию
    по защитному стопу — прогон даёт ненулевой P&L и полный цикл сделки.
    """
    prices = [base + index for index in range(warmup)]
    rising = max(1, run_bars // 4)
    top = prices[-1]
    prices += [top + 1 + index for index in range(rising)]
    fall = run_bars - rising
    prices += [prices[-1] - 2 * (index + 1) for index in range(fall)]
    rows = []
    moment = _aware(START - timedelta(minutes=warmup))
    for price in prices:
        rows.append(
            schemas.HistoricCandle(
                time=moment,
                open=schemas.Quotation(int(price), 0),
                high=schemas.Quotation(int(price) + 1, 0),
                low=schemas.Quotation(int(price) - 1, 0),
                close=schemas.Quotation(int(price) + 1, 0),
                volume=100,
                is_complete=True,
            )
        )
        moment += STEP
    return rows


def _aware(moment: datetime) -> datetime:
    """Эмулятор отдаёт моменты в tz-aware UTC — так же, как T-API."""
    return moment.replace(tzinfo=timezone.utc)


def _future():
    return schemas.Future(
        uid=UID,
        figi="FIGI:NGV6",
        ticker=TICKER,
        name=NAME,
        min_price_increment=schemas.Quotation(1, 0),
        lot=1,
    )


class _StubInstruments:
    def shares(self, **_kw):
        return schemas.SharesResponse(instruments=[])

    def futures(self, **_kw):
        return schemas.FuturesResponse(instruments=[_future()])

    def etfs(self, **_kw):
        return schemas.EtfsResponse(instruments=[])

    def currencies(self, **_kw):
        return schemas.CurrenciesResponse(instruments=[])

    def get_futures_margin(self, *, figi="", instrument_id=""):
        return schemas.FuturesMargin(
            initial_margin_on_futures=schemas.Quotation(0, 0),
            initial_margin_on_delivery=schemas.Quotation(0, 0),
            min_price_increment=schemas.Quotation(1, 0),
        )


class StubEmulatorClient:
    """Клиент с тем же контрактом, что у эмулятора, но без HTTP."""

    def __init__(self, rows):
        self.rows = rows
        self.instruments = _StubInstruments()
        self.requests = []

    def ping(self):
        return {"status": "ok"}

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get_all_candles(
        self, *, from_, to=None, interval=CandleInterval.CANDLE_INTERVAL_UNSPECIFIED,
        figi="", instrument_id="", **_kw,
    ):
        self.requests.append(
            {
                "instrument_id": instrument_id,
                "from": from_,
                "to": to,
                "interval": interval,
            }
        )
        source = self._rows_for(interval)
        for row in source:
            if from_ is not None and row.time < from_:
                continue
            if to is not None and row.time >= to:
                continue
            yield row

    def _rows_for(self, interval):
        """Дневной зонд выбора инструмента обслуживаем отдельным синтетическим баром."""
        if interval == CandleInterval.CANDLE_INTERVAL_DAY:
            return [
                schemas.HistoricCandle(
                    time=_aware(self.rows[-1].time),
                    open=schemas.Quotation(100, 0),
                    high=schemas.Quotation(101, 0),
                    low=schemas.Quotation(99, 0),
                    close=schemas.Quotation(100, 0),
                    volume=1000,
                    is_complete=True,
                )
            ]
        return self.rows


class StubProvider:
    def __init__(self, client):
        self._client = client
        self._client.sync_calls = 0

    def prepare_snapshot(self):
        self._client.sync_calls += 1
        return {
            "producer_id": "robot-db-1", "target_change_id": 12,
            "after_id": 12, "synchronized": True, "snapshot_generation": 42,
        }

    @property
    def base_url(self):
        return "stub://historical"

    def client_context(self, token=None):
        return self._client


class AlwaysBuyStrategy:
    """Стратегия, дающая BUY на каждом баре: детерминированный P&L прогона.

    Регистрируется в реестре стратегий, потому что бот проверяет имена привязок
    при старте — прогон должен идти через настоящий путь валидации.
    """

    NAME = "always_buy"
    STRATEGY_WINDOW = 1

    def __init__(self, config=None):
        self._config = config

    def required_history(self):
        return 1

    def compute(self, df):
        return df

    def decide(self, ta, timeframe=None):
        bar_time = ta["datetime"].iloc[-1]
        return Decision(
            SignalType.BUY,
            float(ta["close"].iloc[-1]),
            bar_time=bar_time,
            event_id=f"always_buy:{timeframe or ''}:{bar_time.isoformat()}:BUY",
            available_at=bar_time,
            timeframe=timeframe,
            strategy_name=self.NAME,
        )


from src.strategies import registry as _registry_module  # noqa: E402

if "always_buy" not in _registry_module._registry:
    _registry_module.register(AlwaysBuyStrategy)


ASSIGNMENT = Assignment(
    id="always_buy-1m",
    strategy="always_buy",
    management=PROFILE,
    timeframe=TF,
)
INSTRUMENT = (NAME, TICKER, "future", NAME)


def _assignment_patch():
    return (
        patch.object(run, "FUTURE_STRATEGIES", {TICKER[:2]: [ASSIGNMENT]}, create=True),
        patch.object(run, "SHARE_STRATEGIES", {}, create=True),
        patch.object(run, "_strategy_map", return_value={"always_buy": AlwaysBuyStrategy()}),
        patch.object(run, "ACTIVE_TIMEFRAMES", frozenset({TF}), create=True),
    )


def _run_history(state_dir, *, pause=0.0, rows=None, start=START, end=END, crash_after=None):
    """Полный исторический прогон через ту же сборку, что и ``run.main``."""
    rows = rows if rows is not None else _series(int((end - start) / STEP))
    client = StubEmulatorClient(rows)
    with contextlib.ExitStack() as stack:
        for item in _assignment_patch():
            stack.enter_context(item)
        stack.enter_context(
            patch.object(run, "EmulatorClientProvider", return_value=StubProvider(client))
        )
        stack.enter_context(patch.object(run, "select_instruments", return_value=[INSTRUMENT]))
        stack.enter_context(patch.object(run, "state_dir_for", return_value=state_dir))
        stack.enter_context(patch.object(run, "setup_logging"))
        stack.enter_context(patch.object(run, "print_contract_metadata"))
        stack.enter_context(
            patch.object(
                run,
                "_load_contracts_metadata",
                return_value={
                    TICKER: ContractMeta(
                        ticker=TICKER, price_step=1.0, step_cost=1.0, go_buy=0.0, go_sell=0.0
                    )
                },
            )
        )
        stack.enter_context(patch("builtins.print", side_effect=print))
        if crash_after is not None:
            stack.enter_context(_interrupting_bot(crash_after))
        code = run._run_history(start, end, pause)
    return code, client


def _interrupting_bot(after_ticks):
    """Бот, который обрывает прогон через ``after_ticks`` тиков (авария Ctrl+C)."""
    from src.bot import TradingBot

    original_run = TradingBot.run

    def patched_run(self):
        state = {"ticks": 0}
        original_tick = self._tick

        def counting_tick(ready_tfs):
            original_tick(ready_tfs)
            state["ticks"] += 1
            if state["ticks"] >= after_ticks:
                raise KeyboardInterrupt

        self._tick = counting_tick
        try:
            original_run(self)
        except KeyboardInterrupt:
            log_free_stop(self)

    return patch.object(TradingBot, "run", patched_run)


def log_free_stop(bot):
    session = bot._run
    if session is not None and not session.should_stop():
        session.mark_data_exhausted()


@pytest.fixture
def live_state_guard(tmp_path):
    """Боевые файлы состояния должны остаться нетронутыми."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "trades.sqlite3").write_bytes(b"")
    (data / "trade_event.csv").write_text("baseline\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in data.iterdir()}
    yield data
    after = {path.name: path.read_bytes() for path in data.iterdir()}
    assert after == before


def _traded_moments(state_dir):
    """Рыночные моменты сделок прогона (момент входа в журнале)."""
    with Storage(Path(state_dir) / run.DATABASE_FILE) as storage:
        return [
            to_naive(row[0]).to_pydatetime()
            for row in storage.connection.execute(
                "SELECT DISTINCT created_at FROM trades ORDER BY created_at"
            )
        ]


class TestHistoricalRunDeterminism:
    def test_shifted_start_reuses_one_import_snapshot(self, tmp_path):
        original = run.run_preflight
        calls = 0

        def shift_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return PreflightReport(start=START + STEP, checked_pairs=1)
            return original(*args, **kwargs)

        with patch.object(run, "run_preflight", side_effect=shift_once):
            code, client = _run_history(tmp_path / "shifted")

        report = json.loads((tmp_path / "shifted" / REPORT_JSON).read_text(encoding="utf-8"))
        assert code == 0
        assert calls == 2
        assert client.sync_calls == 1
        assert report["market_data_sync"]["snapshot_generation"] == 42

    def test_pause_zero_and_one_second_give_same_pnl(self, tmp_path):
        _, _ = _run_history(tmp_path / "p0", pause=0.0)
        _, _ = _run_history(tmp_path / "p1", pause=1.0)

        zero = json.loads((tmp_path / "p0" / REPORT_JSON).read_text(encoding="utf-8"))
        one = json.loads((tmp_path / "p1" / REPORT_JSON).read_text(encoding="utf-8"))

        assert zero["result"] == one["result"]
        assert zero["ticks"] == one["ticks"]

    def test_repeated_run_is_identical(self, tmp_path):
        _run_history(tmp_path / "first")
        _run_history(tmp_path / "second")

        first = json.loads((tmp_path / "first" / REPORT_JSON).read_text(encoding="utf-8"))
        second = json.loads((tmp_path / "second" / REPORT_JSON).read_text(encoding="utf-8"))
        first.pop("journal")
        second.pop("journal")

        assert first == second

    def test_run_actually_traded(self, tmp_path):
        _run_history(tmp_path / "run")

        report = json.loads((tmp_path / "run" / REPORT_JSON).read_text(encoding="utf-8"))
        assert report["result"]["fills"] > 0
        assert report["ticks"] == 10


class TestHistoricalBoundaries:
    def test_last_bar_before_end_processed(self, tmp_path):
        _, _ = _run_history(tmp_path / "run")

        assert _traded_moments(tmp_path / "run")
        assert max(_traded_moments(tmp_path / "run")) < END

    def test_no_request_beyond_end(self, tmp_path):
        _, client = _run_history(tmp_path / "run")

        candle_requests = [r for r in client.requests if r["to"] is not None]
        assert candle_requests
        assert all(
            to_naive(request["to"]).to_pydatetime() <= END for request in candle_requests
        )

    def test_bars_before_start_not_traded(self, tmp_path):
        _, _ = _run_history(tmp_path / "run")

        assert min(_traded_moments(tmp_path / "run")) >= START

    def test_report_states_full_range(self, tmp_path):
        _run_history(tmp_path / "run")

        text = (tmp_path / "run" / REPORT_TXT).read_text(encoding="utf-8")
        assert "2023-06-01 10:00 — 2023-06-01 10:10" in text
        assert "полностью" in text


class TestIsolatedState:
    def test_each_run_checks_import_and_records_snapshot(self, tmp_path):
        _, first_client = _run_history(tmp_path / "a")
        _, second_client = _run_history(tmp_path / "b")

        first = json.loads((tmp_path / "a" / REPORT_JSON).read_text(encoding="utf-8"))
        second = json.loads((tmp_path / "b" / REPORT_JSON).read_text(encoding="utf-8"))
        assert first_client.sync_calls == second_client.sync_calls == 1
        assert first["market_data_sync"]["snapshot_generation"] == 42
        assert second["market_data_sync"]["status"] == "synchronized"

    def test_two_runs_keep_separate_pnl(self, tmp_path):
        _run_history(tmp_path / "a")
        _run_history(tmp_path / "b")

        a = json.loads((tmp_path / "a" / REPORT_JSON).read_text(encoding="utf-8"))
        b = json.loads((tmp_path / "b" / REPORT_JSON).read_text(encoding="utf-8"))
        assert a["result"]["net_pnl"] == b["result"]["net_pnl"] != "0"

    def test_each_run_has_its_own_journal(self, tmp_path):
        _run_history(tmp_path / "a")
        _run_history(tmp_path / "b")

        assert (tmp_path / "a" / run.DATABASE_FILE).exists()
        assert (tmp_path / "b" / run.DATABASE_FILE).exists()

        assert _traded_moments(tmp_path / "a") == _traded_moments(tmp_path / "b")
        assert _traded_moments(tmp_path / "a")

    def test_live_files_untouched(self, tmp_path, live_state_guard):
        with patch.object(run, "runtime_dir", return_value=tmp_path / "data"):
            _run_history(tmp_path / "hist")

    def test_history_state_dir_holds_all_run_files(self, tmp_path):
        _run_history(tmp_path / "hist")

        names = {path.name for path in (tmp_path / "hist").iterdir()}
        assert {run.DATABASE_FILE, REPORT_TXT, REPORT_JSON} <= names


class TestCrashStop:
    def test_crash_reports_market_moment_and_writes_report(self, tmp_path, capsys):
        _run_history(tmp_path / "crash", crash_after=3)

        report = json.loads((tmp_path / "crash" / REPORT_JSON).read_text(encoding="utf-8"))
        assert report["crashed"] is True
        assert report["market_data_sync"]["snapshot_generation"] == 42
        assert report["market_now"].startswith("2023-06-01 10:")
        assert (tmp_path / "crash" / REPORT_TXT).exists()

    def test_crash_line_printed_to_console(self, tmp_path, capsys):
        _run_history(tmp_path / "crash", crash_after=5)

        out = capsys.readouterr().out
        assert "Прогон аварийно остановлен на 2023-06-01 10:" in out

    def test_completion_line_printed_on_normal_finish(self, tmp_path, capsys):
        _run_history(tmp_path / "ok")

        out = capsys.readouterr().out
        assert f"Прогон завершён. Отчёт: {tmp_path / 'ok' / REPORT_TXT}" in out
