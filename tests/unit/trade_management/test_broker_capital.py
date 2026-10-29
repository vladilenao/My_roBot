"""TradeManager контура на брокере (add-tinvest-orders): капитал со счёта, заморозка, снятие привязки."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.trade_journal.storage import Storage
from src.trade_management.errors import RiskStateUnknown
from src.trade_management.manager import (ACCOUNT_UNAVAILABLE, BINDING_REMOVING, INSTRUMENT_FROZEN, TradeManager)


class _Broker:
    def submit(self, action, now):  # pragma: no cover — доставка в этих проверках не нужна
        raise AssertionError


def _manager(tmp_path, capital):
    storage = Storage(tmp_path / "trades.sqlite3", initial_deposit="100000")
    return TradeManager(storage, _Broker(), initial_balance=Decimal("100000"), capital_provider=capital)


def test_budget_base_is_account_capital(tmp_path):
    value = [Decimal("976040.12")]
    manager = _manager(tmp_path, lambda: value[0])
    assert manager._budget_base() == Decimal("976040.12")
    assert manager._budget_state(manager._storage.connection).base == Decimal("976040.12")
    value[0] = None
    with pytest.raises(RiskStateUnknown, match=ACCOUNT_UNAVAILABLE):
        manager._budget_state(manager._storage.connection)


def test_margin_in_use_counts_only_pending_entries_with_account_capital(tmp_path):
    state = SimpleNamespace(pending_margin=Decimal("6228"), used_margin=Decimal("12456"))
    assert _manager(tmp_path, lambda: Decimal(1))._margin_in_use(state) == Decimal("6228")
    emulation = TradeManager(Storage(tmp_path / "emu.sqlite3"), _Broker())
    assert emulation._margin_in_use(state) == Decimal("12456")


def test_frozen_instrument_and_removed_binding_block_entries(tmp_path):
    manager = _manager(tmp_path, lambda: Decimal(1))
    assert manager._entry_block("NGZ6", "future-ng-macd") is None
    manager.freeze_instruments({"NGZ6"})
    assert manager._entry_block("NGZ6", "future-ng-macd") == INSTRUMENT_FROZEN
    manager.freeze_instruments(())
    assert manager.stop_assignment("future-ng-macd", "no_entries") == ()
    assert manager._entry_block("NGZ6", "future-ng-macd") == BINDING_REMOVING
    assert manager._entry_block("NGZ6", "future-ng-flat") is None
    manager.stop_assignment("future-ng-macd", None)
    assert manager._entry_block("NGZ6", "future-ng-macd") is None
