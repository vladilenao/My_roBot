"""Допуск гэпа на входе по фактической цене исполнения.

План риска считается от цены сигнала. Если вход исполнился выше (для BUY) или
ниже (для SELL) этой цены, расстояние до стопа уже потрачено, и сделка стартует
с риском больше admitted. Проверка живёт в ``consume``: единственная точка,
через которую проходит подтверждённое исполнение любого брокера.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.broker.port import BrokerPort, ExecutionEvent, ExecutionStatus
from src.trade_journal.storage import Storage
from src.trade_management.actions import OpenTrade
from src.trade_management.manager import GAP_ENTRY_REASON, TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class GapBroker(BrokerPort):
    """Исполняет вход заданной ценой, чтобы разница с сигналом была точной."""

    def __init__(self, fill: Decimal, market_low: Decimal | None = None) -> None:
        self.fill = fill
        self.market_low = market_low
        self.commands: list = []

    def submit(self, action, now):
        self.commands.append(action)
        return ExecutionEvent(
            f"{action.command_id}:fill", action.command_id, action.command_id, action.trade_id,
            ExecutionStatus.FILL, 1, self.fill, Decimal("0"), now, action.reason,
            market_low=self.market_low,
        )


def _plan(side: str = "BUY") -> TradePlan:
    stop, target = (Decimal("96"), Decimal("104")) if side == "BUY" else (Decimal("104"), Decimal("96"))
    return TradePlan(
        "trade-1", "assignment-1", "NGV6", side, "signal-1", Decimal("100"), stop,
        (TargetPlan("tp-1", target, Decimal("1")),),
        ProfileSnapshot("levels_rr", "1", {}), NOW,
    )


def _entered(storage: Storage, fill: Decimal, side: str = "BUY", tolerance: str = "0.005") -> TradeManager:
    manager = TradeManager(storage, GapBroker(fill), initial_balance=Decimal("1000"),
                           slippage_tolerance=Decimal(tolerance))
    manager.submit_plan(_plan(side), OpenTrade("open-1", "trade-1", 0, "entry", 1))
    manager.dispatch(NOW)
    return manager


def _phase(storage: Storage) -> str:
    return storage.connection.execute(
        "SELECT phase FROM trades WHERE trade_id = 'trade-1'"
    ).fetchone()[0]


def _reasons(storage: Storage) -> list[str]:
    return [
        row[0]
        for row in storage.connection.execute(
            "SELECT reason FROM calculations WHERE algorithm = 'execution.slippage_guard' "
            "AND outcome = 'REJECTED'"
        )
    ]


def _queued(storage: Storage) -> list[str]:
    return [
        row[0]
        for row in storage.connection.execute(
            "SELECT command_id FROM outbox WHERE trade_id = 'trade-1' ORDER BY created_at"
        )
    ]


@pytest.mark.parametrize(("fill", "side"), [(Decimal("102"), "BUY"), (Decimal("98"), "SELL")])
def test_gap_beyond_tolerance_rejects_the_entry(tmp_path, fill, side):
    """Вход за пределами допуска признаётся аварийным: след, отклонение, выход."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _entered(storage, fill, side, tolerance="0.005")

        assert _reasons(storage) == [GAP_ENTRY_REASON]
        assert _phase(storage) == "REJECTED"
        assert any(GAP_ENTRY_REASON in command for command in _queued(storage))


@pytest.mark.parametrize(("fill", "side"), [(Decimal("100.4"), "BUY"), (Decimal("99.6"), "SELL")])
def test_gap_within_tolerance_is_left_alone(tmp_path, fill, side):
    """Уход в пределах допуска — обычное проскальзывание, сделка живёт."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _entered(storage, fill, side, tolerance="0.005")

        assert _reasons(storage) == []
        assert _phase(storage) == "OPEN"
        assert not any(GAP_ENTRY_REASON in command for command in _queued(storage))


@pytest.mark.parametrize(("fill", "side"), [(Decimal("99"), "BUY"), (Decimal("101"), "SELL")])
def test_gap_in_favour_is_not_a_risk(tmp_path, fill, side):
    """Вход выгоднее сигнала риск не увеличивает, поэтому отклонения нет."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        _entered(storage, fill, side, tolerance="0.005")

        assert _reasons(storage) == []
        assert _phase(storage) == "OPEN"


def test_tolerance_is_measured_from_the_signal_price(tmp_path):
    """Допуск — доля от цены сигнала, поэтому один и тот же сдвиг в рублях решает судьбу входа."""
    def opened(storage: Storage, signal: str, fill: str) -> None:
        manager = TradeManager(storage, GapBroker(Decimal(fill)),
                               initial_balance=Decimal("100000"),
                               slippage_tolerance=Decimal("0.005"))
        manager.submit_plan(
            TradePlan(
                "trade-1", "assignment-1", "NGV6", "BUY", "signal-1", Decimal(signal),
                Decimal(signal) - Decimal("4"),
                (TargetPlan("tp-1", Decimal(signal) * 2, Decimal("1")),),
                ProfileSnapshot("levels_rr", "1", {}), NOW,
            ),
            OpenTrade("open-1", "trade-1", 0, "entry", 1),
        )
        manager.dispatch(NOW)

    with Storage(tmp_path / "near.sqlite3") as storage:
        opened(storage, "1000", "1001")
        assert _reasons(storage) == []
        assert _phase(storage) == "OPEN"

    with Storage(tmp_path / "far.sqlite3") as storage:
        opened(storage, "100", "101")
        assert _reasons(storage) == [GAP_ENTRY_REASON]


def test_guard_ignores_a_protective_close(tmp_path):
    """Закрытие позиции не проходит через проверку входа."""
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = _entered(storage, Decimal("100"), "BUY")
        command_id = f"trade-1:pv:stop:{NOW.isoformat()}"
        manager.consume(
            ExecutionEvent(
                f"{command_id}:fill", command_id, command_id, "trade-1",
                ExecutionStatus.FILL, 1, Decimal("95"), Decimal("0"), NOW, "protective",
            )
        )
        assert _reasons(storage) == []

def test_the_fill_price_decides_not_the_extremes_of_its_bar(tmp_path):
    """Бар мог пройти далеко вниз и вернуться; вход состоялся там, где реально купили.

    Отклонение считается от цены исполнения, а не от экстремумов бара: иначе
    вход, случившийся на отскоке, обвинялся бы в гэпе, которого не было.
    """
    with Storage(tmp_path / "trades.sqlite3") as storage:
        manager = TradeManager(
            storage, GapBroker(Decimal("100"), market_low=Decimal("50")),
            initial_balance=Decimal("1000"), slippage_tolerance=Decimal("0.005"),
        )
        manager.submit_plan(_plan("BUY"), OpenTrade("open-1", "trade-1", 0, "entry", 1))
        manager.dispatch(NOW)

        assert _reasons(storage) == []
        assert _phase(storage) == "OPEN"
