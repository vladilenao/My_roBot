from unittest.mock import MagicMock, patch

import pytest

import run
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