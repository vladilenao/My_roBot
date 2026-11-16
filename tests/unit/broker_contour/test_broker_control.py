"""Состав контура на брокере в торговой части (add-tinvest-orders): привязки, снятие, остановка."""

from __future__ import annotations

from src.assembly.core_state import CoreStateStore
from src.broker_contour.bindings import binding_member, resolved
from src.broker_contour.control import BrokerControl, assignment_index
from src.instruments.model import instrument_key
from src.strategies.contracts import Assignment

MACD = Assignment("future-ng-macd", "macd_rsi_stoch", "levels_rr", timeframe="15m")
FLAT = Assignment("future-ng-flat", "flat_triangle", "levels_rr", timeframe="1h")
CONFIG = {"NG": [MACD, FLAT]}
NG = {"label": "NG (Природный газ) — NG-12.26", "ticker": "NGZ6", "instrument_type": "future",
      "short_name": "NG-12.26", "uid": None, "family": "NG"}
CONTRACT = {"NGZ6": {"ticker": "NGZ6", "price_step": 0.001, "step_cost": 8.08664, "go_buy": 6000, "go_sell": 6100,
                     "currency": "RUB", "expiration_date": "2026-12-20T00:00:00"}}


def _card(assignment):
    return {"id": assignment.id, "instrument": "NG", "instrument_type": "future", "strategy": str(assignment.strategy),
            "management": assignment.management, "filter": assignment.filter_profile, "timeframe": assignment.timeframe}


class FakeManager:
    def __init__(self):
        self.calls, self.remainder, self.held = [], {}, None

    def hold_entries(self, held):
        self.held = held

    def stop_contour(self, mode):
        self.calls.append(("contour", mode))

    def stop_assignment(self, assignment_id, mode):
        self.calls.append(("assignment", assignment_id, mode))

    def contour_remainder(self, key=None, *, assignment_id=None):
        return self.remainder.get(assignment_id, (0, 0))


class FakeRoster:
    def __init__(self):
        self.admitted, self.retired, self._keys = [], [], set()

    def keys(self):
        return set(self._keys)

    def warm(self, instrument):
        return True

    def ready(self, instrument):
        return True

    def admit(self, instrument, contracts=None):
        self.admitted.append((instrument.short_name, contracts))
        self._keys.add(instrument_key(instrument))

    def retire(self, instrument):
        self.retired.append(instrument.short_name)
        self._keys.discard(instrument_key(instrument))


def _control(tmp_path, *, busy=False):
    store = CoreStateStore(tmp_path / "core.sqlite3")
    stops, statuses, strategies = [], [], ({}, {})
    control = BrokerControl(core_db=tmp_path / "core.sqlite3", contour_id="sandbox",
                            index=assignment_index({}, CONFIG), strategies=strategies, request_stop=stops.append,
                            status=lambda **fields: statuses.append(fields), broker_busy=lambda: busy)
    manager, roster = FakeManager(), FakeRoster()
    return store, control, manager, roster, stops, statuses, strategies


def test_binding_warms_and_trades_only_its_strategy(tmp_path):
    store, control, manager, roster, _, statuses, (_, future) = _control(tmp_path)
    store.record_launch("sandbox", "sandbox", [], source="api",
                        members=[binding_member(_card(MACD), NG, CONTRACT)])
    control.start(manager, roster)
    assert future == {"NG": [MACD]}
    control.apply()
    control.after_tick()
    assert roster.admitted[0][0] == "NG-12.26" and statuses[-1]["members"][0]["state"] == "trading"
    assert roster.admitted[0][1]["NGZ6"].go_buy == 6000
    assert statuses[-1]["members"][0]["instrument"] == "NG-12.26"


def test_second_binding_of_traded_instrument_gets_its_own_overview(tmp_path):
    store, control, manager, roster, _, statuses, (_, future) = _control(tmp_path)
    store.record_launch("sandbox", "sandbox", [], source="api", members=[binding_member(_card(MACD), NG, CONTRACT)])
    control.start(manager, roster)
    control.apply()
    store.record_bindings("sandbox", "sandbox", add=[binding_member(_card(FLAT), NG, CONTRACT)], remove=(), mode="no_entries",
                          payload={}, expected=None)
    control.apply()      # прогрев
    control.apply()      # стартовый обзор новой привязки
    control.after_tick()
    # Добавленная на ходу привязка допускается со своей карточкой контракта, как и стартовая.
    assert roster.admitted[1][1]["NGZ6"].go_buy == 6000
    assert [a.id for a in future["NG"]] == ["future-ng-macd", "future-ng-flat"] and len(roster.admitted) == 2
    assert {m["key"]: m["state"] for m in statuses[-1]["members"]} == {"future-ng-macd": "trading",
                                                                       "future-ng-flat": "trading"}


def test_removed_binding_drains_then_leaves_with_instrument(tmp_path):
    store, control, manager, roster, _, statuses, (_, future) = _control(tmp_path)
    store.record_launch("sandbox", "sandbox", [], source="api", members=[binding_member(_card(MACD), NG, CONTRACT)])
    control.start(manager, roster)
    control.apply()
    manager.remainder["future-ng-macd"] = (1, 0)
    store.record_bindings("sandbox", "sandbox", add=(), remove=["future-ng-macd"], mode="close_all", payload={},
                          expected=None)
    control.apply()
    control.after_tick()
    assert ("assignment", "future-ng-macd", "close_all") in manager.calls
    assert statuses[-1]["members"][0]["state"] == "closing" and future == {"NG": [MACD]}
    manager.remainder["future-ng-macd"] = (0, 0)
    control.after_tick()
    assert future == {} and roster.retired == ["NG-12.26"] and statuses[-1]["members"] == []
    assert manager.calls[-1] == ("assignment", "future-ng-macd", None)


def test_binding_without_instrument_is_rejected(tmp_path):
    store, control, manager, roster, _, statuses, (_, future) = _control(tmp_path)
    store.record_launch("sandbox", "sandbox", [], source="api", members=[binding_member(_card(MACD))])
    control.start(manager, roster)
    control.after_tick()
    member = statuses[-1]["members"][0]
    assert member["state"] == "rejected" and "добавьте привязку заново" in member["reason"] and future == {}


def test_close_all_finishes_when_nothing_left(tmp_path):
    store, control, manager, roster, stops, statuses, _ = _control(tmp_path, busy=True)
    store.record_launch("sandbox", "sandbox", [], source="api", members=[binding_member(_card(MACD), NG, CONTRACT)])
    control.start(manager, roster)
    store.record_stop("sandbox", "close_all", None)
    control.apply()
    control.after_tick()
    assert ("contour", "close_all") in manager.calls and statuses[-1]["state"] == "stopping_close_all"
    assert stops == []          # у брокера ещё есть заявки
    control._busy = lambda: False
    control.after_tick()
    assert stops == ["остановка командой (close_all)"]


def test_future_binding_without_contract_is_rejected(tmp_path):
    """Без карточки контракта менеджер сделок отклоняет каждый сигнал — привязка не торгует, причина видна."""
    store, control, manager, roster, _, statuses, (_, future) = _control(tmp_path)
    store.record_launch("sandbox", "sandbox", [], source="api", members=[binding_member(_card(MACD), NG)])
    control.start(manager, roster)
    control.after_tick()
    member = statuses[-1]["members"][0]
    assert member["state"] == "rejected" and "Карточка контракта" in member["reason"] and future == {}
    assert not resolved(binding_member(_card(MACD), NG)) and resolved(binding_member(_card(MACD), NG, CONTRACT))
