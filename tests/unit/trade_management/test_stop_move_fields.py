"""Явные поля stop_moved из снимка сделки (console-stop-moved)."""

from decimal import Decimal

from src.trade_management.notification import stop_move_fields


def _visual(*, side="BUY", old="1.1183", new="1.1199", average="1.1232", quantity=4,
            price_step="0.0001", step_cost="8.39497"):
    stop = {"key": "move-1", "time": "2026-10-15T10:00:00", "new": new, "reason": "ma-cloud-protective-stop"}
    if old is not None:
        stop["old"] = old
    data = {"side": side, "event_key": "move-1",
            "stops": ({"key": "entry", "time": "2026-10-15T09:01:00", "new": old or new, "reason": "entry-protection"}, stop),
            "state": {"phase": "OPEN", "quantity": quantity, "stop": new, "targets": ()}}
    if average is not None:
        data["state"]["average_entry"] = average
    if price_step is not None:
        data["price_step"] = price_step
    if step_cost is not None:
        data["step_cost"] = step_cost
    return data


def test_long_move_below_average_is_risk_to_stop():
    assert stop_move_fields(_visual()) == {
        "previous_stop": Decimal("1.1183"), "stop": Decimal("1.1199"), "quantity_remaining": 4,
        "pnl_at_stop": Decimal("-1108.13604"), "pnl_units": "RUB",
    }


def test_short_move_into_profit_has_positive_result():
    fields = stop_move_fields(_visual(side="SELL", old="105", new="98", average="100", quantity=2,
                                      price_step="1", step_cost="1"))
    assert fields["pnl_at_stop"] == Decimal("4")


def test_unknown_step_cost_omits_money():
    fields = stop_move_fields(_visual(step_cost=None))
    assert "pnl_at_stop" not in fields and fields["pnl_units"] == "RAW"
    assert fields["stop"] == Decimal("1.1199") and fields["quantity_remaining"] == 4


def test_missing_previous_stop_is_not_invented():
    fields = stop_move_fields(_visual(old=None))
    assert "previous_stop" not in fields and fields["stop"] == Decimal("1.1199")
