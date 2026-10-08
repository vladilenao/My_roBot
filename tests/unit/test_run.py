from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import json

import pandas as pd
import pytest

import run
from src.bot.run_session import HistoricalRunSession
from src.data.cache import DataExhaustion
from src.history.preflight import PreflightReport
from src.instruments import Instrument, normalize_instrument
from src.portfolio import ContractMeta
from src.scheduler.clock import HistoricalClock
from src.trade_journal.storage import Storage


def _fake_client():
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


@pytest.fixture
def fake_instrument():
    class Instr:
        ticker = "NGV6"
        instrument_type = "future"

    return Instr()


SELECTOR_NG = ("NG (Природный газ) — NG-10.26", "NGV6", "future", "NG-10.26")


def _frame_with_candle():
    return pd.DataFrame({
        "datetime": pd.date_range("2024-01-01", periods=2, freq="1min"),
        "open": [100.0, 100.0],
        "low": [99.0, 99.0],
        "high": [101.0, 101.0],
        "close": [100.5, 100.5],
    })


class TestSmokeCommands:
    def test_version_prints_package_version(self, capsys):
        assert run._run_smoke_command(["--version"])

        assert capsys.readouterr().out == f"{run.__version__}\n"

    def test_config_smoke_uses_bundled_default(self, monkeypatch, capsys):
        loaded = {}

        def load_config(defaults, config_file, bundled_file):
            loaded.update(defaults=defaults, config_file=config_file, bundled_file=bundled_file)

        monkeypatch.setattr("src.config_loader.load_config", load_config)

        assert run._run_smoke_command(["--config-smoke"])

        assert loaded["bundled_file"].name == "default.toml"
        assert loaded["config_file"].name == "missing-robot.toml"
        assert capsys.readouterr().out == "Конфигурация по умолчанию загружена.\n"

    def test_unrecognized_arguments_are_not_smoke_commands(self):
        assert not run._run_smoke_command(["--no-prompt"])


class TestLoadContractsMetadata:
    def test_sets_contracts_from_context_client(self, fake_instrument):
        """Метаданные должны браться через `client_context` (Services-фасад), а не `get_client`."""
        fake_client = _fake_client()
        with patch("src.api.client.client_context", return_value=fake_client) as ctx, patch(
            "src.api.instruments.load_futures_contracts",
            return_value={"NGV6": ContractMeta("NGV6", 0.001, 8.4, 100, 100)},
        ) as loader, patch("run.TINKOFF_TOKEN", "t"):
            contracts = run._load_contracts_metadata([fake_instrument])

        ctx.assert_called_once_with()
        loader.assert_called_once_with(fake_client, tickers={"NGV6"})
        assert contracts["NGV6"].price_step == 0.001

    def test_selector_tuples_use_real_ticker(self):
        """`(display, ticker, type[, name])` из селектора → в запрос идёт реальный тикер (индекс 1)."""
        assert run._instrument_ticker(SELECTOR_NG) == "NGV6"
        assert run._instrument_ticker(("RTS", "RIU6", "future")) == "RIU6"
        assert run._instrument_ticker(SELECTOR_NG[:1]) == "NG (Природный газ) — NG-10.26"
        assert run._instrument_ticker(None) is None

    def test_empty_without_token(self, fake_instrument):
        with patch("run.TINKOFF_TOKEN", ""), patch("run.log") as log:
            assert run._load_contracts_metadata([fake_instrument]) == {}
        log.warning.assert_called_once()

    def test_no_instruments_skips_source_entirely(self):
        with patch("run.TINKOFF_TOKEN", ""), patch("run.log") as log:
            assert run._load_contracts_metadata([]) == {}
        log.warning.assert_not_called()

    def test_exception_falls_back_to_empty(self, fake_instrument):
        with patch("src.api.client.client_context", side_effect=RuntimeError("boom")), patch(
            "run.TINKOFF_TOKEN", "t"
        ):
            assert run._load_contracts_metadata([fake_instrument]) == {}


    def test_share_and_future_metadata_are_merged(self):
        fake_client = _fake_client()
        with patch("src.api.client.client_context", return_value=fake_client), patch(
            "src.api.instruments.load_futures_contracts",
            return_value={"NGV6": ContractMeta("NGV6", 0.001, 8.4, 100, 100)},
        ) as futures, patch(
            "src.api.instruments.load_share_contracts",
            return_value={"SBER": ContractMeta("SBER", 0.01, 1.0, 0.0, 0.0)},
        ) as shares, patch("run.TINKOFF_TOKEN", "t"):
            contracts = run._load_contracts_metadata(
                [SELECTOR_NG, ("Сбербанк", "SBER", "share", "SBER")]
            )

        futures.assert_called_once_with(fake_client, tickers={"NGV6"})
        shares.assert_called_once_with(fake_client, tickers={"SBER"})
        assert set(contracts) == {"NGV6", "SBER"}
        assert contracts["SBER"].price_step == 0.01
        assert contracts["SBER"].step_cost == 1.0
        assert contracts["SBER"].go_buy == 0.0

    def test_missing_metadata_is_reported(self, caplog):
        fake_client = _fake_client()
        with patch("src.api.client.client_context", return_value=fake_client), patch(
            "src.api.instruments.load_futures_contracts", return_value={}
        ), patch("src.api.instruments.load_share_contracts", return_value={}), patch(
            "run.TINKOFF_TOKEN", "t"
        ):
            contracts = run._load_contracts_metadata(
                [SELECTOR_NG, ("Сбербанк", "SBER", "share", "SBER")]
            )

        assert contracts == {}
        assert "SBER" in caplog.text
        assert "NGV6" in caplog.text

    def test_one_failing_type_keeps_other_metadata(self):
        fake_client = _fake_client()
        with patch("src.api.client.client_context", return_value=fake_client), patch(
            "src.api.instruments.load_futures_contracts",
            return_value={"NGV6": ContractMeta("NGV6", 0.001, 8.4, 100, 100)},
        ), patch(
            "src.api.instruments.load_share_contracts",
            side_effect=RuntimeError("каталог акций недоступен"),
        ), patch("run.TINKOFF_TOKEN", "t"):
            contracts = run._load_contracts_metadata(
                [SELECTOR_NG, ("Сбербанк", "SBER", "share", "SBER")]
            )

        assert set(contracts) == {"NGV6"}

    def test_history_source_is_used_instead_of_token(self):
        fake_client = _fake_client()
        provider = MagicMock()
        provider.client_context.return_value = fake_client
        with patch("src.api.instruments.load_share_contracts", return_value={}) as shares:
            run._load_contracts_metadata(
                [("Сбербанк", "SBER", "share", "SBER")], client_provider=provider
            )

        shares.assert_called_once_with(fake_client, tickers={"SBER"})


class TestHistoryMetadataWithoutToken:
    """Исторический режим не должен требовать `TINKOFF_TOKEN`."""

    def _provider(self, fake_client):
        provider = MagicMock()
        provider.client_context.return_value = fake_client
        return provider

    def test_futures_and_shares_load_with_no_token(self):
        fake_client = _fake_client()
        provider = self._provider(fake_client)
        with patch("run.TINKOFF_TOKEN", None), patch(
            "src.api.instruments.load_futures_contracts",
            return_value={"SiH5": ContractMeta("SiH5", 1.0, 1.0, 12345.67, 12345.67)},
        ) as futures, patch(
            "src.api.instruments.load_share_contracts",
            return_value={"SBER": ContractMeta("SBER", 0.01, 1.0, 0.0, 0.0)},
        ) as shares, patch(
            "run._token_client_context",
            side_effect=AssertionError("токен не должен быть нужен"),
        ):
            contracts = run._load_contracts_metadata(
                [("Фьючерс", "SiH5", "future", "Si-6.25"),
                 ("Сбербанк", "SBER", "share", "SBER")],
                client_provider=provider,
            )

        futures.assert_called_once_with(fake_client, tickers={"SiH5"})
        shares.assert_called_once_with(fake_client, tickers={"SBER"})
        assert set(contracts) == {"SiH5", "SBER"}
        assert contracts["SiH5"].go_buy == 12345.67

    def test_entries_not_rejected_for_missing_metadata(self, caplog):
        fake_client = _fake_client()
        provider = self._provider(fake_client)
        with patch("run.TINKOFF_TOKEN", None), patch(
            "src.api.instruments.load_futures_contracts",
            return_value={"SiH5": ContractMeta("SiH5", 1.0, 1.0, 12345.67, 12345.67)},
        ), patch(
            "src.api.instruments.load_share_contracts",
            return_value={"SBER": ContractMeta("SBER", 0.01, 1.0, 0.0, 0.0)},
        ):
            contracts = run._load_contracts_metadata(
                [("Фьючерс", "SiH5", "future", "Si-6.25"),
                 ("Сбербанк", "SBER", "share", "SBER")],
                client_provider=provider,
            )

        assert "no-contract-metadata" not in caplog.text
        assert "входы отклонятся" not in caplog.text
        assert set(contracts) == {"SiH5", "SBER"}

    def test_no_token_warning_absent_when_provider_available(self, caplog):
        fake_client = _fake_client()
        provider = self._provider(fake_client)
        with patch("run.TINKOFF_TOKEN", None), patch(
            "src.api.instruments.load_futures_contracts", return_value={}
        ), patch("src.api.instruments.load_share_contracts", return_value={}):
            run._load_contracts_metadata(
                [("Фьючерс", "SiH5", "future", "Si-6.25")], client_provider=provider
            )

        assert "TINKOFF_TOKEN" not in caplog.text


class TestRuntimeComposition:
    def test_notify_only_never_initializes_storage_or_broker(self):
        with patch("run.trading_enabled", return_value=False), patch(
            "src.trade_journal.storage.Storage"
        ) as storage, patch("src.broker.create_addressable_journal_broker") as broker:
            runtime = run._build_runtime([], MagicMock(), MagicMock(), run.runtime_dir())

        assert runtime.trade_manager is None
        assert runtime.post_tick is None
        assert not hasattr(runtime, "execution")
        storage.assert_not_called()
        broker.assert_not_called()

    def test_simulation_uses_sqlite_and_addressed_broker_without_legacy_adapter(self):
        broker = MagicMock()
        broker.drain_events.return_value = []
        manager = MagicMock()
        storage = MagicMock()
        bus = MagicMock()
        storage.load_trades.return_value = ()
        with patch("run.trading_enabled", return_value=True), patch(
            "src.trade_journal.storage.Storage", return_value=storage
        ) as storage_cls, patch(
            "src.broker.create_addressable_journal_broker", return_value=broker
        ) as broker_factory, patch(
            "src.trade_management.manager.TradeManager", return_value=manager
        ) as manager_cls, patch("run._load_contracts_metadata", return_value={}), patch(
            "run.print_contract_metadata"
        ), patch("run._risk_limits") as limits:
            runtime = run._build_runtime([], MagicMock(), bus, run.runtime_dir())

        storage_cls.assert_called_once_with(
            run.runtime_dir() / run.DATABASE_FILE,
            journal_path=run.runtime_dir() / run.JOURNAL_FILE,
            positions_path=run.runtime_dir() / run.POSITIONS_FILE,
            audit_path=run.runtime_dir() / run.AUDIT_FILE,
            audit_max_bytes=run.AUDIT_MAX_BYTES,
            audit_backup_count=run.AUDIT_BACKUP_COUNT,
            initial_deposit=str(run.INITIAL_DEPOSIT),
            clock=None,
        )
        broker_factory.assert_called_once_with(
            run.INITIAL_DEPOSIT, run.CLEARING_TIMES, contract_names={}
        )
        manager_cls.assert_called_once_with(
            storage,
            broker,
            initial_balance=run.Decimal(str(run.INITIAL_DEPOSIT)),
            budget_observer=ANY,
            execution_observer=ANY,
            profiles_config=run.TRADE_MANAGEMENT_PROFILES,
            risk_limits=limits.return_value,
            max_qty=run.RISK_LIMITS.get("max_qty"),
            commission=run.RISK_LIMITS.get("commission"),
            slippage=run.RISK_LIMITS.get("slippage"),
            slippage_tolerance=run.RISK_LIMITS.get("slippage_tolerance"),
            min_trade_risk_pct=run.RISK_LIMITS.get("min_trade_risk_pct"),
            portfolio_pct=run.RISK_LIMITS.get("portfolio_pct", 2),
            min_risk_cost_ratio=run.RISK_LIMITS.get("min_risk_cost_ratio", 2),
            min_net_payoff=run.RISK_LIMITS.get("min_net_payoff", 1.5),
            max_slippage_r=run.RISK_LIMITS.get("max_slippage_r", 0.25),
            contract_expiry_block_days=run.CONTRACT_EXPIRY_BLOCK_DAYS,
            direction_limits=run.TRADING_DIRECTIONS,
            clock=None,
            signal_filter=ANY,
        )
        manager.restore.assert_called_once_with()
        limits.assert_called_once_with()
        assert runtime.trade_manager is manager
        assert not hasattr(runtime, "risk_manager")
        assert not hasattr(runtime, "execution")

        callbacks = manager_cls.call_args.kwargs
        callbacks["budget_observer"]({"risk_state": "unknown", "unknown_reason": "нет подтверждённого стопа"})
        alert = bus.publish.call_args.args[0]
        assert alert.type is run.EventType.RISK_LIMIT_HIT and alert.get("risk_scope") == "portfolio"
        assert "trade_id" not in alert.payload
        callbacks["execution_observer"](SimpleNamespace(trade_id="trade", timestamp=datetime(2026, 1, 1), filled_quantity=2,
            price=run.Decimal(100), fee=run.Decimal(0), fee_source="broker", reason="entry", execution_id="exec", status="fill"),
            {"action_type": "OPEN", "instrument_id": "SBER", "side": "BUY", "gross_pnl": run.Decimal(0), "net_pnl": run.Decimal(0),
             "fees_total": run.Decimal(0), "fees_known": True, "pnl_units": "RUB", "quantity_remaining": 2})
        fact = bus.publish.call_args.args[0]
        assert fact.type is run.EventType.TRADE_OPENED and fact.get("fee_source") == "broker"
        assert fact.get("fee") == 0 and fact.get("quantity_remaining") == 2
        assert fact.get("side") == "BUY"

    def test_build_runtime_normalizes_selector_tuples_before_consumers(self):
        broker = MagicMock()
        broker.drain_events.return_value = []
        broker.drain_addressed_events.return_value = []
        manager = MagicMock()
        storage = MagicMock()
        storage.load_trades.return_value = ()
        cache = MagicMock()
        cache.frame_for.return_value = _frame_with_candle()
        with patch("run.trading_enabled", return_value=True), patch(
            "src.trade_journal.storage.Storage", return_value=storage
        ), patch(
            "src.broker.create_addressable_journal_broker", return_value=broker
        ), patch(
            "src.trade_management.manager.TradeManager", return_value=manager
        ), patch("run._load_contracts_metadata", return_value={}), patch(
            "run.print_contract_metadata"
        ), patch("run._risk_limits"):
            runtime = run._build_runtime([SELECTOR_NG], cache, MagicMock(), run.runtime_dir())

        storage.set_names.assert_called_once_with({"NGV6": "NG-10.26"})
        broker.set_names.assert_called_once_with({"NGV6": "NG-10.26"})

        runtime.post_tick(("1m",))

        instrument = cache.frame_for.call_args.args[0]
        assert isinstance(instrument, Instrument)
        assert instrument.ticker == "NGV6"
        assert instrument.short_name == "NG-10.26"
        broker.track_bar.assert_called_once()


class TestInstrumentNames:
    def test_share_named_by_ticker(self):
        names = run._instrument_names(
            [normalize_instrument(("SBER", "SBER", "share")), normalize_instrument(("GAZP", "GAZP", "share"))]
        )

        assert names == {"SBER": "SBER", "GAZP": "GAZP"}

    def test_future_keeps_contract_name(self):
        instruments = [
            normalize_instrument(("NG (Природный газ) — NG-9.26", "NGV6", "future", "NG-9.26")),
            normalize_instrument(("NG (Природный газ) — NG-10.26", "NGV7", "future")),
        ]

        assert run._instrument_names(instruments) == {"NGV6": "NG-9.26"}


SHIFTED_START = datetime(2024, 1, 3, 4, 2)
SELECTOR_SBER = ("Сбербанк", "SBER", "share")


def _preflight_report(*, start, ok=True, shift_to=None):
    return PreflightReport(
        problems=() if ok else ("SBER 1m: свечи не покрывают конец диапазона",),
        notes=() if shift_to is None else ("начало диапазона сдвинуто",),
        start=shift_to or start,
        shift_to=shift_to,
        checked_pairs=2,
        detail=["SBER 1m: пусто", "SBER 1h: пусто"],
    )


class TestHistoryStartShift:
    """Проверка границ может перенести начало: прогон собирается заново."""

    START = datetime(2024, 1, 1, 0, 0)
    END = datetime(2024, 1, 5, 0, 0)

    def _launch(self, tmp_path, report):
        session = MagicMock(start=self.START, end=self.END)
        with (
            patch.object(run, "setup_logging"),
            patch.object(run, "select_instruments", return_value=[SELECTOR_SBER]) as selector,
            patch.object(run, "active_pairs", return_value=[("SBER", "1m")]),
            patch.object(run, "_warmup_bars", return_value=41),
            patch.object(run, "run_preflight", return_value=report),
            patch.object(run, "build_channels") as channels,
        ):
            code, shifted, instruments = run._launch(
                state_dir=tmp_path / "state",
                client_provider=MagicMock(),
                clock=MagicMock(),
                session=session,
                channel_names=[],
                history=True,
            )
        return code, shifted, instruments, selector, channels

    def test_launch_hands_shift_back_without_starting_bot(self, tmp_path):
        code, shifted, instruments, _selector, channels = self._launch(
            tmp_path, _preflight_report(start=self.START, shift_to=SHIFTED_START)
        )

        assert (code, shifted) == (3, SHIFTED_START)
        assert [item.ticker for item in instruments] == ["SBER"]
        channels.assert_not_called()

    def test_launch_reuses_instruments_of_previous_attempt(self, tmp_path):
        ready = [normalize_instrument(SELECTOR_SBER)]

        with (
            patch.object(run, "setup_logging"),
            patch.object(run, "select_instruments") as selector,
            patch.object(run, "active_pairs", return_value=[("SBER", "1m")]),
            patch.object(run, "_warmup_bars", return_value=41),
            patch.object(
                run, "run_preflight",
                return_value=_preflight_report(start=self.START, ok=False),
            ),
            patch.object(run, "build_channels") as channels,
        ):
            code, shifted, instruments = run._launch(
                state_dir=tmp_path / "state",
                client_provider=MagicMock(),
                clock=MagicMock(),
                session=MagicMock(start=self.START, end=self.END),
                channel_names=[],
                history=True,
                instruments=ready,
            )

        selector.assert_not_called()
        channels.assert_not_called()
        assert (code, shifted, instruments) == (2, None, ready)


class TestRunHistoryRetry:
    """Повтор исторического прогона: один раз, с теми же инструментами."""

    START = datetime(2024, 1, 1, 0, 0)
    END = datetime(2024, 1, 5, 0, 0)

    def _run(self, tmp_path):
        with (
            patch.object(run, "HistoricalClock"),
            patch.object(run, "build_run_session"),
            patch.object(run, "_timeframes", return_value={"1m"}),
            patch.object(run, "smallest_period", return_value=timedelta(minutes=1)),
            patch.object(run, "EmulatorClientProvider"),
            patch.object(run, "state_dir_for", side_effect=lambda s, e: tmp_path / f"{s:%m%d%H%M}"),
        ):
            return run._run_history(self.START, self.END, 0.0)

    def test_rejected_attempt_is_dropped_and_range_rerun(self, tmp_path):
        calls = []

        def launch(**kwargs):
            calls.append(kwargs)
            return [(3, SHIFTED_START, ["ready"]), (0, None, ["ready"])][len(calls) - 1]

        with (
            patch.object(run, "_launch", side_effect=launch),
        ):
            code = self._run(tmp_path)

        assert code == 0
        assert [call["state_dir"].name for call in calls] == ["01010000", "01030402"]
        assert calls[0]["instruments"] is None
        assert calls[1]["instruments"] == ["ready"]
        assert not (tmp_path / "01010000").exists()

    def test_plain_run_is_not_repeated(self, tmp_path):
        with patch.object(run, "_launch", return_value=(2, None, ["ready"])) as launcher:
            code = self._run(tmp_path)

        assert code == 2
        assert launcher.call_count == 1

    def test_endless_shift_stops_after_one_retry(self, tmp_path):
        with patch.object(run, "_launch", return_value=(3, SHIFTED_START, ["ready"])) as launcher:
            code = self._run(tmp_path)

        assert code == 3
        assert launcher.call_count == 2


class TestFinishHistoryReportsCheckedHorizon:
    """Границы проверенного горизонта доезжают из сессии в отчёт прогона."""

    def _session(self, exhaustion):
        clock = HistoricalClock(
            datetime(2026, 9, 25, 12, 0),
            datetime(2026, 10, 10, 12, 0),
            timedelta(minutes=1),
            0.0,
            sleeper=lambda secs: None,
        )
        session = HistoricalRunSession(clock)
        for _ in range(5):
            clock.advance()
        session.mark_data_exhausted(exhaustion)
        return session

    def _finish(self, tmp_path, session):
        cache = MagicMock(
            missed_bars=7, gaps=1, longest_gap=pd.Timedelta(hours=6)
        )
        timeline = MagicMock()
        timeline.now.return_value = pd.Timestamp("2026-09-25 12:05")
        state_dir = tmp_path / "run-1"

        run._finish_history(
            Storage(tmp_path / "trades.sqlite3", initial_deposit="100000"),
            session,
            cache,
            timeline,
            state_dir,
        )
        payload = json.loads((state_dir / "report.json").read_text(encoding="utf-8"))
        text = (state_dir / "report.txt").read_text(encoding="utf-8")
        return payload, text

    def test_limited_horizon_is_reported_as_limited(self, tmp_path):
        session = self._session(
            DataExhaustion(
                at=pd.Timestamp("2026-09-25 12:05"),
                horizon_start=pd.Timestamp("2026-09-25 12:05"),
                horizon_end=pd.Timestamp("2026-10-02 12:05"),
                range_end=pd.Timestamp("2026-10-10 12:00"),
                horizon_limited=True,
            )
        )

        payload, text = self._finish(tmp_path, session)

        assert payload["covered"] is False
        assert payload["crashed"] is False
        assert payload["checked_horizon"] == {
            "start": "2026-09-25 12:05",
            "end": "2026-10-02 12:05",
            "limited": True,
        }
        assert "Проверенный горизонт: 2026-09-25 12:05 — 2026-10-02 12:05" in text
        assert "ограничен семью сутками" in text
        assert "следующий бар не найден в проверенном горизонте" in payload["stop_reason"]

    def test_full_remainder_is_reported_as_checked(self, tmp_path):
        session = self._session(
            DataExhaustion(
                at=pd.Timestamp("2026-09-25 12:05"),
                horizon_start=pd.Timestamp("2026-09-25 12:05"),
                horizon_end=pd.Timestamp("2026-10-10 12:00"),
                range_end=pd.Timestamp("2026-10-10 12:00"),
                horizon_limited=False,
            )
        )

        payload, text = self._finish(tmp_path, session)

        assert payload["checked_horizon"]["limited"] is False
        assert "проверен весь остаток диапазона" in payload["stop_reason"]
        assert "ограничен семью сутками" not in text
