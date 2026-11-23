"""``probe-stops`` и ``probe-trigger``: стоп-заявка далеко от рынка и срабатывание по подтверждению."""

from __future__ import annotations

from decimal import Decimal

from src.finam.orders import is_active
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from tests.unit.finam.fakes import NG


def stops(broker) -> list:
    return [state for state in broker.orders.values() if state.order.type == orders_pb2.ORDER_TYPE_STOP]


def test_stop_probe_places_and_cancels(run, broker):
    broker.trigger_after_polls = None
    result = run("probe-stops", "NG")
    assert result.code == 0, result.report()
    for code in ("Ф0.20", "Ф0.21", "Ф0.22", "Ф0.23", "Ф0.24", "Ф0.25", "Ф0.99"):
        assert result.row(code)[0] == "да", (code, result.row(code))
    assert "стоп покупка по 3.3 при последней 3" in result.row("Ф0.20")[1]
    assert "GOOD_TILL_CANCEL" in result.row("Ф0.21")[1] and "LAST_UP" in result.row("Ф0.21")[1]
    assert not any(is_active(state) for state in stops(broker))


def test_stop_probe_sell_side(run, broker):
    broker.trigger_after_polls = None
    result = run("probe-stops", "NG", "--side", "sell", "--offset-pct", "5")
    assert "стоп продажа по 2.85" in result.row("Ф0.20")[1]
    assert "LAST_DOWN" in result.row("Ф0.21")[1]


def test_stop_probe_keep(run, broker):
    broker.trigger_after_polls = None
    result = run("probe-stops", "NG", "--keep", "--demo")
    assert result.row("Ф0.25") == ("не проверено", "оставлена по --keep")
    assert "cleanup --demo" in result.report()
    assert [state.status for state in stops(broker)] == [orders_pb2.ORDER_STATUS_WATCHING]


def test_trigger_trades_and_closes(run, broker):
    result = run("probe-trigger", "NG", "--demo")
    assert "Введите ДА" in result.text
    assert result.code == 0, result.report()
    for code in ("Ф0.30", "Ф0.31", "Ф0.32", "Ф0.33", "Ф0.34", "Ф0.35", "Ф0.36", "Ф0.99"):
        assert result.row(code)[0] == "да", (code, result.row(code))
    assert "стоп покупка по 3.001" in result.row("Ф0.30")[1]
    assert "triggered_order_id" in result.row("Ф0.31")[1]
    assert "комиссия 1.5 RUB" in result.row("Ф0.33")[1]
    assert "позиция до 0, после 1" in result.row("Ф0.34")[1]
    assert broker.positions[NG] == Decimal(0)


def test_trigger_not_reached(run, broker):
    broker.trigger_after_polls = None
    result = run("probe-trigger", "NG", "--demo", "--wait", "1")
    outcome, detail = result.row("Ф0.31")
    assert outcome == "не проверено" and "цена не дошла" in detail
    assert result.row("Ф0.35") == ("не проверено", "стоп не сработал")
    assert not any(is_active(state) for state in stops(broker))
    assert broker.positions.get(NG, Decimal(0)) == Decimal(0)


def test_trigger_cleanup_closes_only_own_volume(run, broker):
    broker.positions[NG] = Decimal(2)  # позиция владельца до разведки
    result = run("probe-trigger", "NG", "--real")
    assert result.code == 0, result.report()
    assert broker.positions[NG] == Decimal(2)


def test_stop_probe_time_in_force(run, broker):
    broker.trigger_after_polls = None
    result = run("probe-stops", "NG", "--keep", "--tif", "gtc")
    assert "--tif gtc" in result.report()
    [stop] = stops(broker)
    assert stop.order.time_in_force == orders_pb2.TIME_IN_FORCE_GOOD_TILL_CANCEL
    assert "time_in_force TIME_IN_FORCE_GOOD_TILL_CANCEL" in result.row("Ф0.21")[1]


def test_probes_do_not_touch_kept_stop_of_earlier_run(run, broker):
    """23.11: уборка второго probe-stops сняла стоп, оставленный первым по --keep."""
    broker.trigger_after_polls = None
    run("probe-stops", "NG", "--keep")
    [kept] = stops(broker)
    broker.no_money = True  # второй стоп не встаёт, как с --tif gtc 23.11
    run("probe-stops", "NG", "--keep", "--side", "sell")
    broker.no_money = False
    run("probe-stops", "NG")
    run("probe", "NG")
    assert broker.orders[kept.order_id].status == orders_pb2.ORDER_STATUS_WATCHING
    result = run("cleanup")
    assert broker.orders[kept.order_id].status == orders_pb2.ORDER_STATUS_CANCELED
    assert "снято заявок разведки: 1" in result.text
