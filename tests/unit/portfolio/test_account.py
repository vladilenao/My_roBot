import pytest

from src.portfolio import Account, ContractMeta


class TestAccount:
    def test_starts_with_initial_deposit(self):
        acc = Account(100000)
        assert acc.balance == 100000
        assert acc.equity == 100000

    def test_realize_grows_balance(self):
        acc = Account(100000)
        acc.realize(500)
        acc.realize(-100)
        assert acc.balance == pytest.approx(100400)

    def test_floating_moves_equity_not_balance(self):
        acc = Account(100000)
        acc.realize(100)
        acc.set_floating_pnl(350.5)
        assert acc.balance == pytest.approx(100100)
        assert acc.equity == pytest.approx(100450.5)

    def test_clearing_resets_cycle_and_floating(self):
        acc = Account(100000)
        acc.realize(120)
        acc.set_floating_pnl(80)
        acc.clear()
        assert acc.realized_cycle == 0.0
        assert acc.floating == 0.0
        assert acc.balance == pytest.approx(100120)

    def test_snapshot_fields(self):
        acc = Account(100000)
        acc.realize(50)
        acc.set_floating_pnl(20)
        snap = acc.snapshot()
        assert snap["balance"] == 100050
        assert snap["equity"] == 100070


class TestContractMeta:
    def test_position_value(self):
        meta = ContractMeta(ticker="NG", price_step=0.5, step_cost=75, go_buy=1000, go_sell=900)
        assert meta.position_value(2, 10.0) == pytest.approx(2 * (10.0 / 0.5) * 75)

    def test_position_value_zero_step(self):
        meta = ContractMeta(ticker="X", price_step=0, step_cost=0, go_buy=0, go_sell=0)
        assert meta.position_value(5, 100.0) == 0.0