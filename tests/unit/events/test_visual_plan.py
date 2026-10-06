from datetime import datetime
from decimal import Decimal

from src.events.event import Event
from src.events.visual import VisualSnapshot, validate, visual_schema
from src.instruments import Instrument
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan
from src.trade_management.notification import signal_snapshot


def test_complete_plan_snapshot_schema_and_precision_round_trip():
    plan = TradePlan("t", "a", "SBER", "BUY", "s", Decimal("300.00"), Decimal("297.00"),
                     (TargetPlan("tp1", Decimal("303.40"), Decimal("0.5")),
                      TargetPlan("tp2", Decimal("306.40"), Decimal("0.5"))),
                     ProfileSnapshot("levels_rr", "1", {}), datetime(2026, 1, 1), timeframe="15m")
    snapshot = signal_snapshot(plan, 3, Instrument("Сбер", "SBER", "share", "SBER"))
    payload = Event.signal("SBER", side="BUY", quantity=3, entry=plan.reference_entry,
                           stop=plan.stop_price, visual=snapshot).to_dict()["payload"]["visual"]
    validate(payload, visual_schema())
    assert payload["plan"]["entry"] == "300.00"
    assert sum(t["quantity"] for t in payload["plan"]["targets"]) == 3
    assert payload["unit"] == "lot"
    assert VisualSnapshot(payload).to_dict() == payload
