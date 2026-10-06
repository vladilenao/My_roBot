"""Пересчёт целей и стопа от фактической средней цены позиции.

План риска считается от цены сигнала, а вход исполняется по цене бара. Разница
съедает расстояние до стопа до того, как сделка начала жить, поэтому цели и
стоп должны быть названы от средней, но с сохранением исходной геометрии:
иначе каждый добор раздвигал бы сетку всё дальше.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.portfolio.models import ContractMeta
from src.trade_journal.storage import Storage
from src.trade_management.actions import AddToTrade, OpenTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan, rebase_on_average

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class PriceFillBroker(BrokerPort):
    """Исполняет входы заданными ценами по очереди."""

    def __init__(self, fills: list[Decimal]) -> None:
        self.fills = list(fills)

    def contract_for(self, instrument):
        return ContractMeta(instrument, 1, 1, 0, 0)

    def submit(self, action, now):
        fill = self.fills.pop(0) if self.fills else Decimal("100")
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, action.quantity, fill, Decimal("0"), now, action.reason,
        )


def _plan() -> TradePlan:
    return TradePlan(
        "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal("100"), Decimal("96"),
        (TargetPlan("tp-1", Decimal("104"), Decimal("0.5"), Decimal("4")),
         TargetPlan("tp-2", Decimal("110"), Decimal("0.5"), Decimal("10"))),
        ProfileSnapshot("levels_rr", "1", {}), NOW,
    )


def _manager(storage: Storage, fills: list[Decimal]) -> TradeManager:
    manager = TradeManager(storage, PriceFillBroker(fills), initial_balance=Decimal("1000"),
                           slippage_tolerance=Decimal("1"))
    manager.submit_plan(_plan(), OpenTrade("open-1", "trade-1", 0, "entry", 1),
                        price_step=Decimal(1), step_cost=Decimal(1))
    manager.dispatch(NOW)
    return manager


def _target_prices(storage: Storage) -> list[str]:
    return [
        row[0]
        for row in storage.connection.execute(
            "SELECT price FROM targets WHERE trade_id = 'trade-1' ORDER BY target_index"
        )
    ]


def _confirmed_stop(storage: Storage) -> str | None:
    return storage.connection.execute(
        "SELECT confirmed_stop FROM protection WHERE trade_id = 'trade-1'"
    ).fetchone()[0]


def _plan_prices(storage: Storage) -> list[str]:
    import json
    payload = json.loads(
        storage.connection.execute("SELECT plan_json FROM trades WHERE trade_id = 'trade-1'").fetchone()[0]
    )
    return [target.get("initial_step") for target in payload["targets"]]


def test_targets_and_stop_follow_the_filled_average(tmp_path):
    """Вход дороже сигнала двигает цели и стоп на ту же дельту."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _manager(storage, [Decimal("102")])

        assert _target_prices(storage) == ["106", "112"]
        assert _confirmed_stop(storage) == "98"


def test_favourable_entry_rebases_the_other_way(tmp_path):
    """Вход дешевле сигнала сдвигает геометрию вниз, а не отбрасывает её."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _manager(storage, [Decimal("98")])

        assert _target_prices(storage) == ["102", "108"]
        assert _confirmed_stop(storage) == "94"


def test_plan_is_not_rewritten_by_a_fill(tmp_path):
    """Записанный план остаётся тем, на чём сделка была допущена."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _manager(storage, [Decimal("102")])

        assert _plan_prices(storage) == ["4", "10"]


def test_second_add_does_not_compound_the_step(tmp_path):
    """Повторный пересчёт от новой средней не раздвигает сетку дальше."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, [Decimal("102"), Decimal("98")])
        manager.submit_action(AddToTrade("add-1", "trade-1", 1, "add", 1, "limit", Decimal("98")))
        manager.dispatch(NOW)

        # average is (102 + 98) / 2 = 100 — back to the signal price exactly
        assert _target_prices(storage) == ["104", "110"]
        assert _confirmed_stop(storage) == "96"


def test_filled_target_keeps_the_price_of_record(tmp_path):
    """Исполненная цель не переписывается, даже когда средняя изменилась."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, [Decimal("100")])
        storage.connection.execute(
            "UPDATE targets SET filled_quantity = 1, status = 'FILLED' WHERE target_id = 'tp-1'"
        )
        storage.connection.commit()
        manager.submit_action(AddToTrade("add-1", "trade-1", 1, "add", 1, "limit", Decimal("100")))
        manager.dispatch(NOW)

        prices = _target_prices(storage)
        assert prices[0] == "104"
        assert prices[1] == "110"


def test_rebase_is_a_pure_function_of_plan_and_average():
    """Обе стороны считают одно и то же из одних и тех же входных данных."""
    plan = _plan()
    rebased = rebase_on_average(plan, Decimal("102"))

    assert rebased.reference_entry == Decimal("102")
    assert rebased.stop_price == Decimal("98")
    assert [target.price for target in rebased.targets] == [Decimal("106"), Decimal("112")]
    assert plan.reference_entry == Decimal("100")
    assert [target.price for target in plan.targets] == [Decimal("104"), Decimal("110")]


def test_rebase_at_the_signal_price_is_a_no_op():
    """Вход ровно по сигналу не должен двигать ничего."""
    plan = _plan()
    rebased = rebase_on_average(plan, Decimal("100"))

    assert rebased.stop_price == plan.stop_price
    assert [target.price for target in rebased.targets] == [
        target.price for target in plan.targets
    ]


def test_legacy_plan_without_step_derives_it_from_its_prices(tmp_path):
    """План, записанный до появления поля, восстанавливает шаг из своих цен."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _manager(storage, [Decimal("100")])
        storage.connection.execute(
            "UPDATE trades SET plan_json = replace(plan_json, ', \"initial_step\": \"4\"', '') "
            "WHERE trade_id = 'trade-1'"
        )
        storage.connection.commit()
        recovered = next(item for item in manager.restore() if item.plan.trade_id == "trade-1")

        assert recovered.plan.targets[0].initial_step == Decimal("4")
        assert recovered.plan.targets[1].initial_step == Decimal("10")


def test_broker_and_journal_agree_on_rebased_target_prices(tmp_path):
    """Эмулятор и журнал должны называть одну и ту же цену цели."""
    from src.broker import JournalBroker
    from src.portfolio import PositionManager
    from src.trade_journal import TradeJournal

    plan = _plan()
    with Storage(tmp_path / "trades.sqlite3") as storage:
        broker = JournalBroker(
            TradeJournal.created_on_init(tmp_path / "journal.csv"),
            PositionManager(initial_deposit=Decimal("1000"), max_risk_pct=2),
            ["14:05"],
        )
        manager = TradeManager(storage, broker, initial_balance=Decimal("1000"),
                               slippage_tolerance=Decimal("1"))
        manager.submit_plan(plan, OpenTrade("open-1", "trade-1", 0, "entry", 1))
        manager.dispatch(NOW)
        broker.track_bar(NOW + timedelta(minutes=1), {"NGV6": (103.0, 102.0, 103.0, 103.0)}, {})
        for event in broker.drain_addressed_events():
            manager.consume(event)

        simulator = broker.trade_state("trade-1").plan
        recovered = next(item for item in manager.restore() if item.plan.trade_id == "trade-1")

        assert [target.price for target in simulator.targets] == [
            target.price for target in recovered.plan.targets
        ]
        assert simulator.stop_price == recovered.state.confirmed_stop
