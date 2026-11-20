"""Стоп у брокера (add-broker-protection): хранитель стопа, срабатывание, «стоп раньше выхода», простой."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from src.broker.port import ExecutionStatus
from src.broker_contour.api import OrderFailure
from src.broker_contour.broker import ExitPolicy, TInvestBroker
from src.broker_contour.orders import OrderLedger, order_key
from src.broker_contour.stops import ACTIVE, EXECUTED, LOST, UNKNOWN, StopLedger
from src.events.types import EventType
from src.trade_management.actions import AddToTrade, CloseTrade, EntryOrderType, MoveStop, OpenTrade, ReduceTrade
from tests.unit.broker_contour.fake_api import NG, NG_META, NOW, Clock, FakeApi
from tests.unit.broker_contour.test_tinvest_broker import _plan

D = Decimal
STOP = D("2.813")          # стоп плана 2.812 после пересчёта от средней 2.846


class Harness:
    def __init__(self, tmp_path, *, journal=None, api=None, clock=None):
        self.api, self.clock, self.published = api or FakeApi(), clock or Clock(), []
        self.journal = set() if journal is None else journal
        self.ledger = OrderLedger(tmp_path / "broker.sqlite3")
        self.stops = StopLedger(self.ledger)
        self.broker = TInvestBroker(self.api, self.ledger, contour="sandbox",
                                    policy=ExitPolicy(5, 15, 2, 60, stop_place_secs=30),
                                    publish=self.published.append, journal_has=self.journal.__contains__,
                                    clock=self.clock, stops=self.stops)
        self.broker.set_contracts({NG: NG_META})
        self.broker.set_names({NG: "NG-12.26"})

    def drain(self):
        events = self.broker.drain_addressed_events()
        self.journal.update(event.execution_id for event in events)
        return events

    def service(self, seconds: float = 0):
        self.broker.service(self.clock.advance(seconds))

    def filled(self, lots=2, price="2.846", trade_id="trade-ng-1", targets=True):
        plan = _plan(trade_id) if targets else replace(_plan(trade_id), targets=())
        self.broker.register_trade(plan)
        action = OpenTrade(f"{trade_id}:entry", trade_id, 0, "signal", lots, EntryOrderType.LIMIT, D("2.8457"))
        self.broker.submit(action, self.clock())
        self.broker.ingest(self.api.fill(order_key(action.command_id), lots, price))
        self.drain()
        return order_key(action.command_id)

    def stop_events(self):
        return [(e.get("kind"), e.get("lots"), str(e.get("price"))) for e in self.published
                if e.type is EventType.BROKER_ORDER and e.get("order") == "broker_stop"]

    def active(self):
        return [(r.side, r.lots, r.stop_price) for r in self.api.active_stops()]

    def call_index(self, name, start=0):
        return next(i for i, call in enumerate(self.api.calls) if i >= start and call[0] == name)

    def target_keys(self):
        return [call[2] for call in self.api.posts("limit") if ":tp:" in self._command(call[2])]

    def _command(self, key):
        row = self.ledger.get(key)
        return row.command_id if row else ""


def test_stop_placed_after_entry(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.service()
    assert h.active() == [("SELL", 2, STOP)]
    assert h.stop_events() == [("stop_placed", 2, "2.813")]
    assert h.broker.broker_stop_covers("trade-ng-1")
    h.service(5)
    assert [c for c in h.api.calls if c[0] == "post_stop"].__len__() == 1      # повторно не ставится


def test_target_fill_replaces_stop_without_two_active(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.service()
    first = h.target_keys()[0]
    mark = len(h.api.calls)
    h.broker.ingest(h.api.fill(first, 1, "2.912"))
    h.drain()
    h.service()
    assert h.active() == [("SELL", 1, STOP)]
    assert h.call_index("cancel_stop", mark) < h.call_index("post_stop", mark)
    assert h.stop_events()[-1][:2] == ("stop_placed", 1)
    rows = h.stops.rows(trade_id="trade-ng-1")
    assert [r.state for r in rows] == ["cancelled", ACTIVE] and rows[-1].reason == "target"


def test_move_stop_replaces_price(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.service()
    h.broker.submit(MoveStop("move-1", "trade-ng-1", 1, "breakeven", D("2.846")), h.clock())
    h.service()
    assert h.active() == [("SELL", 2, D("2.846"))]
    assert h.stops.rows(trade_id="trade-ng-1")[-1].reason == "move"


def test_add_rejected_until_stop_in_sync(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    add = AddToTrade("add-1", "trade-ng-1", 1, "add", 1, EntryOrderType.LIMIT, D("2.850"))
    assert h.broker.submit(add, h.clock()).reason == "stop-pending"      # стоп ещё не поставлен
    h.service()
    add2 = AddToTrade("add-2", "trade-ng-1", 1, "add", 1, EntryOrderType.LIMIT, D("2.850"))
    assert h.broker.submit(add2, h.clock()).status is ExecutionStatus.ACK


def test_partial_entry_stop_on_filled_then_grows(tmp_path):
    h = Harness(tmp_path)
    h.broker.register_trade(_plan())
    action = OpenTrade("trade-ng-1:entry", "trade-ng-1", 0, "signal", 3, EntryOrderType.LIMIT, D("2.8457"))
    h.broker.submit(action, h.clock())
    key = order_key(action.command_id)
    h.broker.ingest(h.api.fill(key, 1, "2.846"))
    h.drain()
    h.service()
    assert h.active() == [("SELL", 1, STOP)]
    h.broker.ingest(h.api.fill(key, 2, "2.846"))
    h.drain()
    h.service()
    assert [(side, lots) for side, lots, _ in h.active()] == [("SELL", 3)]


def test_stop_not_placed_exits_and_alarms(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.api.stop_failures.append(OrderFailure("rejected", "INVALID_ARGUMENT", "30042", "неверная цена"))
    h.service()
    assert h.active() == [] and not h.api.posts("limit")[1:]
    h.service(31)
    exits = [call for call in h.api.posts("limit") if call[3] == "SELL"]
    assert len(exits) == 1 and exits[0][4] == 2                     # выход циклом С3 от последней цены
    assert ("stop_failed", 2, "2.813") in h.stop_events()


def test_market_closed_retries_without_exit(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.api.stop_failures.append(OrderFailure("market_closed", "FAILED_PRECONDITION", "30079"))
    h.service()
    h.service(31)
    assert not [call for call in h.api.posts("limit") if call[3] == "SELL"]
    h.service(30)                                                  # минута после отказа — повтор
    assert h.active() == [("SELL", 2, STOP)]


def test_minute_through_stop_ignored_while_broker_stop_active(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.service()
    h.broker.track_bar(NOW + timedelta(minutes=5), {NG: (2.83, 2.80, 2.84, 2.81)})
    h.service(1)
    assert not [call for call in h.api.posts("limit") if call[3] == "SELL"]


def test_minute_through_stop_exits_when_no_broker_stop(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)                       # стоп-заявка ещё не поставлена — условие у контура
    h.broker.track_bar(NOW + timedelta(minutes=5), {NG: (2.83, 2.80, 2.84, 2.81)})
    h.service(1)
    exits = [call for call in h.api.posts("limit") if call[3] == "SELL"]
    assert [(c[4], c[5]) for c in exits] == [(2, D("2.808"))]
    assert h.active() == []                       # стоп-задание контура — стоп-заявки нет


def test_broker_trigger_closes_trade_and_cancels_targets(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.service()
    targets = h.target_keys()
    stop_id = h.api.active_stops()[0].stop_order_id
    report = h.api.trigger(stop_id, "2.810")
    assert h.broker.ingest(report) is False                       # нашего ключа нет — незнакомый отчёт
    h.service(1)
    events = h.drain()
    assert [(e.command_id, e.filled_quantity, e.reference_price) for e in events] == [
        (f"trade-ng-1:pv:stop:{stop_id}:exch-0002", 2, STOP)]
    assert h.broker.trade_state("trade-ng-1").position == 0
    cancelled = {call[1] for call in h.api.calls if call[0] == "cancel"}
    assert {h.api.orders[k].broker_order_id for k in targets} <= cancelled
    assert not [call for call in h.api.posts() if call[3] == "SELL" and call[4] == 2]
    assert ("stop_triggered", 2, "2.813") in h.stop_events()
    assert h.stops.rows(trade_id="trade-ng-1")[-1].state == EXECUTED


def test_partial_market_stop_rest_by_exit_cycle(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.service()
    stop_id = h.api.active_stops()[0].stop_order_id
    report = h.api.trigger(stop_id, "2.810")
    partial = replace(report, status="cancelled", lots_executed=1, trades=report.trades[:1])
    partial = replace(partial, trades=(replace(report.trades[0], quantity=1),))
    h.api.orders[f"stop-fill:{report.broker_order_id}"] = partial
    h.broker.ingest(partial)
    h.service(1)
    h.drain()
    h.service(1)
    assert h.broker.trade_state("trade-ng-1").position == 1
    rest = [call for call in h.api.posts("limit") if call[3] == "SELL"]
    assert [c[4] for c in rest] == [1]


def test_close_waits_for_stop_cancellation(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.service()
    mark = len(h.api.calls)
    h.broker.submit(CloseTrade("close-1", "trade-ng-1", 2, "signal-exit"), h.clock())
    assert not h.api.posts()[1:]                                  # стоп ещё стоит — выход ждёт
    h.service()
    assert h.call_index("cancel_stop", mark) < h.call_index("post", mark)
    exit_post = h.api.posts("limit")[-1]
    assert (exit_post[3], exit_post[4]) == ("SELL", 2) and h.active() == []
    assert ("stop_removed", 2, "2.813") in h.stop_events()


def test_close_when_stop_already_executed(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.service()
    stop_id = h.api.active_stops()[0].stop_order_id
    report = h.api.trigger(stop_id, "2.810")                       # сработал, отчёт стрима ещё не пришёл
    h.broker.submit(CloseTrade("close-1", "trade-ng-1", 2, "signal-exit"), h.clock())
    h.service(1)                                                    # снятие: NOT_FOUND — уточняем опросом
    h.service(3)
    h.broker.ingest(report)
    h.drain()
    h.service(3)
    events = h.drain()
    assert h.broker.trade_state("trade-ng-1").position == 0
    assert ("close-1", ExecutionStatus.REJECT, "trade-not-open") in [(e.command_id, e.status, e.reason) for e in events]
    assert not [call for call in h.api.posts() if call[3] == "SELL"]


def test_reduce_keeps_stop_plus_exit_within_position(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.service()
    mark = len(h.api.calls)
    h.broker.submit(ReduceTrade("reduce-1", "trade-ng-1", 2, "partial", 1), h.clock())
    assert not h.api.posts()[1:]
    h.service()
    assert h.active() == [("SELL", 1, STOP)]
    exits = [call for call in h.api.posts("limit") if call[3] == "SELL"]
    assert [c[4] for c in exits] == [1]
    assert h.call_index("cancel_stop", mark) < h.call_index("post", mark)


def test_account_position_caps_stop(tmp_path):
    h = Harness(tmp_path)
    h.filled(lots=10, targets=False)
    h.service()
    h.broker.set_account_positions({NG: 6})                         # 4 лота проданы вручную
    h.service()
    assert h.active() == [("SELL", 6, STOP)]


def test_lost_post_found_by_attributes(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.api.stop_failures.append("accepted-lost")
    h.service()
    row = h.stops.rows(trade_id="trade-ng-1")[-1]
    assert row.state == UNKNOWN
    h.service(3)
    assert h.stops.get(row.key).state == ACTIVE and len(h.api.stops) == 1


def test_lost_post_two_candidates_stays_unknown(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.api.stop_failures.append("accepted-lost")
    h.service()
    twin = next(iter(h.api.stops.values()))
    h.api.stops["stop-order-9999"] = replace(twin, stop_order_id="stop-order-9999")
    h.service(3)
    assert h.stops.rows(trade_id="trade-ng-1")[-1].state == UNKNOWN
    assert h.broker.in_flight_instruments() >= {NG}


def test_stop_price_in_rubles_matched(tmp_path):
    h = Harness(tmp_path)
    h.api.stop_price_rub = True
    h.filled(targets=False)
    h.api.stop_failures.append("accepted-lost")
    h.service()
    h.service(3)
    assert h.stops.rows(trade_id="trade-ng-1")[-1].state == ACTIVE


def _resume(h, lots=2):
    state = SimpleNamespace(quantity=lots, average_price=D("2.846"), target_prices={}, confirmed_stop=STOP)
    h.broker.register_trade(_plan())
    h.broker.resume_trade(SimpleNamespace(plan=_plan(), state=state, entry_quantity=lots, target_filled={},
                                          pending_increases=(), pending_exits=()))


def test_stop_executed_while_down_closed_before_first_tick(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.service()
    stop_id = h.api.active_stops()[0].stop_order_id
    h.api.trigger(stop_id, "2.810")                                # контур не работал, отчёт стрима потерян
    h.ledger.close()
    h2 = Harness(tmp_path, journal=h.journal, api=h.api, clock=h.clock)
    _resume(h2)
    h2.broker.recover(h2.clock())
    events = h2.drain()
    assert [(e.command_id, e.filled_quantity) for e in events] == [(f"trade-ng-1:pv:stop:{stop_id}:exch-0002", 2)]
    h2.service(1)
    assert h2.broker.trade_state("trade-ng-1").position == 0 and h2.active() == []
    assert not h2.broker.busy() or h2.ledger.orders(live=True)     # цели сняты, ждут подтверждения


def test_trade_without_stop_gets_one_after_restart(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)                                        # сборка С3: стоп-заявки не было
    h.ledger.close()
    h2 = Harness(tmp_path, journal=h.journal, api=h.api, clock=h.clock)
    _resume(h2)
    h2.broker.recover(h2.clock())
    assert h2.drain() == []                                        # разбор очереди при старте: вход уже в журнале
    h2.service()
    assert h2.active() == [("SELL", 2, STOP)]
    assert h2.stops.rows(trade_id="trade-ng-1")[-1].reason == "restart"


def test_entry_waiting_through_restore_keeps_entry_reason(tmp_path):
    h = Harness(tmp_path)
    h.broker.register_trade(_plan())
    action = OpenTrade("trade-ng-1:entry", "trade-ng-1", 0, "signal", 2, EntryOrderType.LIMIT, D("2.8457"))
    h.broker.submit(action, h.clock())
    _resume(h, lots=0)                                             # restore застал лимит входа, рестарта не было
    h.broker.ingest(h.api.fill(order_key(action.command_id), 2, "2.846"))
    h.drain()
    h.service()
    assert h.stops.rows(trade_id="trade-ng-1")[-1].reason == "entry"


def test_restore_between_fill_and_stop_keeps_entry_reason(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    _resume(h)                                                     # restore прошёл до первой постановки стопа
    h.service()
    assert h.stops.rows(trade_id="trade-ng-1")[-1].reason == "entry"


def test_process_exit_leaves_stop_and_close_all_removes(tmp_path):
    h = Harness(tmp_path)
    h.filled(targets=False)
    h.service()
    assert h.broker.busy() and h.broker.live_stop_count() == 1
    h.broker.submit(CloseTrade("close-1", "trade-ng-1", 2, "close-all"), h.clock())
    h.service()
    exit_post = h.api.posts("limit")[-1]
    h.broker.ingest(h.api.fill(exit_post[2], 2, "2.846"))
    h.drain()
    h.service(1)
    assert h.api.active_stops() == [] and not h.broker.busy()


def test_disabled_stops_keep_contour_condition(tmp_path):
    """Без журнала стоп-заявок брокер ведёт себя как на С3: стоп-заявок нет, добор не ждёт стопа."""
    from tests.unit.broker_contour.test_tinvest_broker import Harness as C3

    h = C3(tmp_path)
    h.filled()
    h.broker.service(h.clock())
    assert not [c for c in h.api.calls if c[0] in ("post_stop", "stop_orders")]
    add = AddToTrade("add-1", "trade-ng-1", 1, "add", 1, EntryOrderType.LIMIT, D("2.850"))
    assert h.broker.submit(add, h.clock()).status is ExecutionStatus.ACK


def test_stale_account_snapshot_does_not_cut_new_entry_stop(tmp_path):
    """Снимок счёта до входа показывает 0 — стоп нового входа не срезается (только снимок после сделки)."""
    h = Harness(tmp_path)
    h.broker.set_account_positions({NG: 0}, at=h.clock())
    h.clock.advance(1)
    h.filled(targets=False)
    h.service()
    assert h.active() == [("SELL", 2, STOP)]


def test_order_lines_for_broker_stop():
    from src.events.event import Event
    from src.notifier.templates.orders import order_line

    def line(kind, **fields):
        return order_line(Event.broker_order(kind, contour="sandbox", trade_id="t", event_id="e", instrument="NG",
                                             order="broker_stop", order_type="stop", side="SELL", **fields))

    assert line("stop_placed", lots=10, price=D("2.901"), broker_order="ab12") == \
        "стоп у брокера 2.901 · 10 лотов, стоп-заявка …ab12"
    assert line("stop_placed", lots=5, price=D("2.901"), broker_order="cd34", replaced=True, reason="target") == \
        "стоп пересоздан на остаток 5 лотов (цель): 2.901, стоп-заявка …cd34"
    assert line("stop_placed", lots=10, price=D("2.916"), replaced=True, reason="move") == "стоп перенесён: 2.916 · 10 лотов"
    assert line("stop_triggered", lots=10, price=D("2.901"), exchange_order="91c0") == \
        "стоп сработал у брокера, заявка …91c0"
    assert line("stop_failed", lots=10, price=D("2.901"), reason="broker:30042") == \
        "⚠️ стоп не принят брокером: broker:30042 — выход из сделки"
    assert line("stop_removed", lots=10, price=D("2.901"), broker_order="ab12") == "стоп снят перед выходом, стоп-заявка …ab12"


def test_stopped_message_counts_broker_stops():
    from src.events.event import Event
    from src.notifier.telegram_templates import render_broker_account

    def text(**fields):
        return render_broker_account(Event.broker_account("stopped", contour="sandbox", account="…09c3",
                                                          event_id="e", reason="Ctrl+C", **fields))

    assert "стоп-заявок у брокера: 2 — позиции защищены без контура" in text(open_positions=2, broker_stops=2)
    assert "не у всех позиций стоп у брокера" in text(open_positions=2, broker_stops=1)
    assert "стоп без контура не проверяется" in text(open_positions=1)


def test_reconcile_compares_own_stops_with_account():
    """Своя стоп-заявка совпадает со счётом; лишняя на счёте — расхождение ``stop_order`` (задача 9.3)."""
    from src.broker_contour.account import AccountSnapshot
    from src.broker_contour.reconcile import JournalView, compare

    journal = JournalView(holdings=(("NG-12.26", 2),), stop_orders=("NG-12.26",))
    own = AccountSnapshot(D(1_000_000), None, (("NG-12.26", 2),), (), ("NG-12.26",))
    assert compare(own, journal) == []
    foreign = AccountSnapshot(D(1_000_000), None, (("NG-12.26", 2),), (), ("NG-12.26", "NG-12.26"))
    [item] = compare(foreign, journal)
    assert (item["instrument"], item["what"], item["account"], item["journal"]) == ("NG-12.26", "stop_order", 2, 1)


# --- потерянный ответ без повтора ключа (fix-stop-key-duplicate) ---------------------------

def _lost_harness(tmp_path):
    h = Harness(tmp_path)
    h.ledger.clock = h.clock                     # время строк журнала — часы теста, как у брокера
    h.filled(targets=False)
    h.api.stop_failures.append("accepted-lost-hidden")
    h.service()
    return h, h.stops.rows(trade_id="trade-ng-1")[-1]


def _posted_keys(h):
    return [call[1] for call in h.api.calls if call[0] == "post_stop"]


def test_lost_stop_waits_then_new_key_and_duplicate_cancelled(tmp_path):
    h, lost = _lost_harness(tmp_path)
    assert lost.state == UNKNOWN
    h.service(3)
    assert h.stops.get(lost.key).state == UNKNOWN and len(_posted_keys(h)) == 1    # 15 с ещё не прошли
    h.service(13)
    assert h.stops.get(lost.key).state == LOST
    keys = _posted_keys(h)
    assert len(keys) == 2 and len(set(keys)) == 2                                   # новый ключ, не повтор
    assert h.stops.rows(trade_id="trade-ng-1")[-1].state == ACTIVE
    assert NG in h.broker.in_flight_instruments()                                   # сверка не сравнивает
    h.api.hidden_stops.clear()                                                      # первая показалась у брокера
    h.service(3)
    row = h.stops.get(lost.key)
    assert row.state == "cancelled" and row.reason == "duplicate" and row.own_cancel
    assert h.active() == [("SELL", 2, STOP)]                                        # у брокера одна
    assert h.stop_events()[-1][0] == "stop_duplicate_cancelled"
    assert NG not in h.broker.in_flight_instruments()


def test_lost_stop_never_arrives_becomes_undelivered(tmp_path):
    h, lost = _lost_harness(tmp_path)
    h.api.stops.pop(next(iter(h.api.hidden_stops)))                                 # до брокера не дошла
    h.api.hidden_stops.clear()
    h.service(16)
    assert h.stops.get(lost.key).state == LOST and h.active() == [("SELL", 2, STOP)]
    h.service(60 * 10)
    row = h.stops.get(lost.key)
    assert row.state == "cancelled" and row.reason == "undelivered"
    assert len(_posted_keys(h)) == 2 and len(set(_posted_keys(h))) == 2


def test_duplicate_cancel_unconfirmed_resolved_by_poll(tmp_path):
    h, lost = _lost_harness(tmp_path)
    h.service(16)
    h.api.hidden_stops.clear()
    h.api.cancel_stop_failures.append(OrderFailure("unknown", "DEADLINE_EXCEEDED"))
    h.service(3)
    assert h.stops.get(lost.key).state == "cancelling"
    h.service(3)
    assert h.stops.get(lost.key).state == "cancelled" and h.active() == [("SELL", 2, STOP)]


def test_restart_with_sending_row_follows_new_rule(tmp_path):
    h, lost = _lost_harness(tmp_path)
    h.stops.update(lost.key, state="sending")
    h.clock.advance(20)
    h.broker.recover_stops(h.clock())
    assert h.stops.get(lost.key).state == LOST                                      # не найдена за 15 с
    h.service()
    assert len(set(_posted_keys(h))) == 2                                            # новая — новым ключом
    h.api.hidden_stops.clear()                                                      # прежняя показалась
    h.service(3)
    assert h.active() == [("SELL", 2, STOP)] and h.stops.get(lost.key).reason == "duplicate"


def test_duplicate_cancelled_card_line(tmp_path):
    from src.notifier.templates.orders import order_line

    h, _ = _lost_harness(tmp_path)
    h.service(16)
    h.api.hidden_stops.clear()
    h.service(3)
    event = next(e for e in h.published if e.get("kind") == "stop_duplicate_cancelled")
    assert order_line(event).startswith("лишняя стоп-заявка снята, стоп-заявка …")
