from unittest.mock import ANY, MagicMock, patch

import pytest

import run
from src.execution import NotifyOnlyExecutionPort
from src.portfolio import ContractMeta


def _fake_client():
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


@pytest.fixture
def fake_instrument():
    class Instr:
        ticker = "NGV6"

    return Instr()


SELECTOR_NG = ("NG (Природный газ) — NG-10.26", "NGV6", "future", "NG-10.26")


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

    def test_empty_without_token(self):
        with patch("run.TINKOFF_TOKEN", ""), patch("run.log") as log:
            assert run._load_contracts_metadata([]) == {}
        log.warning.assert_called_once()

    def test_exception_falls_back_to_empty(self, fake_instrument):
        with patch("src.api.client.client_context", side_effect=RuntimeError("boom")), patch(
            "run.TINKOFF_TOKEN", "t"
        ):
            assert run._load_contracts_metadata([fake_instrument]) == {}


class TestRuntimeComposition:
    def test_notify_only_never_initializes_storage_or_broker(self):
        notifier = MagicMock()
        with patch("run.trading_enabled", return_value=False), patch(
            "src.trade_journal.storage.Storage"
        ) as storage, patch("src.broker.create_addressable_journal_broker") as broker:
            runtime = run._build_runtime([], notifier, MagicMock())

        assert isinstance(runtime.execution, NotifyOnlyExecutionPort)
        assert runtime.trade_manager is None
        storage.assert_not_called()
        broker.assert_not_called()

    def test_simulation_uses_sqlite_and_addressed_broker_without_legacy_adapter(self):
        notifier = MagicMock()
        broker = MagicMock()
        broker.drain_events.return_value = []
        manager = MagicMock()
        storage = MagicMock()
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
            runtime = run._build_runtime([], notifier, MagicMock())

        storage_cls.assert_called_once_with(
            run.runtime_dir() / run.DATABASE_FILE,
            journal_path=run.runtime_dir() / run.JOURNAL_FILE,
            positions_path=run.runtime_dir() / run.POSITIONS_FILE,
            audit_path=run.runtime_dir() / run.AUDIT_FILE,
            audit_max_bytes=run.AUDIT_MAX_BYTES,
            audit_backup_count=run.AUDIT_BACKUP_COUNT,
        )
        broker_factory.assert_called_once_with(
            run.INITIAL_DEPOSIT, run.CLEARING_TIMES, contract_names={}
        )
        manager_cls.assert_called_once_with(
            storage,
            broker,
            initial_balance=run.Decimal(str(run.INITIAL_DEPOSIT)),
            profiles_config=run.TRADE_MANAGEMENT_PROFILES,
            risk_limits=limits.return_value,
            max_qty=run.RISK_LIMITS.get("max_qty"),
            commission=run.RISK_LIMITS.get("commission"),
            slippage=run.RISK_LIMITS.get("slippage"),
            signal_filter=ANY,
        )
        manager.restore.assert_called_once_with()
        limits.assert_called_once_with()
        assert runtime.trade_manager is manager
        assert runtime.risk_manager.__class__.__name__ == "PortfolioRiskManager"
        assert isinstance(runtime.execution, NotifyOnlyExecutionPort)
