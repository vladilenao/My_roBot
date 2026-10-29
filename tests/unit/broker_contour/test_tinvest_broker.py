"""``TInvestBroker`` (add-tinvest-orders): заявки, журнал заявок, восстановление по ключу, выходы лимитом."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from src.broker.port import ExecutionStatus
from src.broker_contour.api import OrderFailure
from src.broker_contour.broker import ExitPolicy, TInvestBroker
from src.broker_contour.orders import CANCELLED, CANCELLING, OrderLedger, order_key
from src.broker_contour.prices import PriceCard, entry_limit, exit_limit, points_from_rub, trade_lots
from src.events.types import EventType
from src.trade_management.actions import CancelEntry, CloseTrade, EntryOrderType, MoveStop, OpenTrade, ReduceTrade
from src.trade_management.models import (CURRENT_ALGORITHM, CostSnapshot, ProfileSnapshot, TargetPlan, TradePlan)
from tests.unit.broker_contour.fake_api import NG, NG_META, NOW, Clock, FakeApi

D = Decimal


def _plan(trade_id="trade-ng-1", side="BUY") -> TradePlan:
    sign = D(1) if side == "BUY" else D(-1)
    return TradePlan(
        trade_id, "future-ng-macd", NG, side, "signal-1", D("2.845"), D("2.845") - sign * D("0.033"),
        (TargetPlan("tp-1", D("2.845") + sign * D("0.066"), D("0.5"), D("0.066")),
         TargetPlan("tp-2", D("2.845") + sign * D("0.132"), D("0.5"), D("0.132"))),
        ProfileSnapshot("levels_rr", "1", {}), NOW, timeframe="15m", algorithm_version=CURRENT_ALGORITHM,
        cost_snapshot=CostSnapshot(D("12"), D("0")), entry_order_type=EntryOrderType.LIMIT, price_step=D("0.001"))


class Harness:
    def __init__(self, tmp_path, *, journal=None):
        self.api, self.clock, self.published = FakeApi(), Clock(), []
        self.journal = set() if journal is None else journal
        self.ledger = OrderLedger(tmp_path / "broker.sqlite3")
        self.broker = TInvestBroker(self.api, self.ledger, contour="sandbox", policy=ExitPolicy(5, 15, 2, 60),
                                    publish=self.published.append, journal_has=self.journal.__contains__,
                                    clock=self.clock)
        self.broker.set_contracts({NG: NG_META})
        self.broker.set_names({NG: "NG-12.26"})

    def open(self, lots=2, trade_id="trade-ng-1"):
        self.broker.register_trade(_plan(trade_id))
        action = OpenTrade(f"{trade_id}:entry", trade_id, 0, "signal", lots, EntryOrderType.LIMIT, D("2.8457"))
        return action, self.broker.submit(action, self.clock())

    def drain(self):
        events = self.broker.drain_addressed_events()
        self.journal.update(event.execution_id for event in events)
        return events

    def kinds(self):
        return [event.get("kind") for event in self.published if event.type is EventType.BROKER_ORDER]

    def filled(self, lots=2, price="2.846"):
        action, _ = self.open(lots)
        key = order_key(action.command_id)
        self.broker.ingest(self.api.fill(key, lots, price))
        self.drain()
        return key


def test_rubles_to_points_and_rounding():
    card = PriceCard(NG, D("0.001"), D("8.08664"))
    assert points_from_rub(card, D("22731.54504")) == D("2.811")
    assert entry_limit(card, D("2.8457"), "BUY") == D("2.845")
    assert entry_limit(card, D("2.8451"), "SELL") == D("2.846")
    assert exit_limit(card, D("2.812"), "SELL", 5) == D("2.807")
    assert exit_limit(card, D("2.812"), "BUY", 5) == D("2.817")
    share = PriceCard("uid-sber", D("0.01"), D("0.1"), lot=10, future=False)
    assert trade_lots(share, [20, 10], 3) == [2, 1]
    assert trade_lots(share, [2, 1], 3) == [2, 1]


def test_entry_accepted_then_partial_and_full_fill(tmp_path):
    h = Harness(tmp_path)
    action, answer = h.open()
    assert answer.status is ExecutionStatus.ACK and answer.order_id == "broker-order-0001"
    post = h.api.posts()[0]
    assert post[2] == order_key(action.command_id) and post[5] == D("2.845") and post[4] == 2
    assert h.kinds() == ["sent"] and any(e.type is EventType.ORDER_ACCEPTED for e in h.published)
    key = order_key(action.command_id)
    h.broker.ingest(h.api.fill(key, 1, "2.845"))
    first = h.drain()
    assert [(e.status, e.filled_quantity, e.price) for e in first] == [(ExecutionStatus.PARTIAL, 1, D("2.845"))]
    h.broker.ingest(h.api.fill(key, 1, "2.847"))
    second = h.drain()
    assert second[0].status is ExecutionStatus.FILL and second[0].execution_id.startswith("tinvest:")
    assert h.broker.trade_state("trade-ng-1").position == 2
    # Повтор стрима и stages той же сделки биржи — без нового исполнения.
    h.broker.ingest(h.api.orders[key])
    assert h.drain() == []


def test_repeated_submit_does_not_post_again(tmp_path):
    h = Harness(tmp_path)
    action, _ = h.open()
    again = h.broker.submit(action, h.clock())
    assert again.status is ExecutionStatus.ACK and len(h.api.posts()) == 1


def test_market_entry_and_out_of_band_rejected_without_post(tmp_path):
    h = Harness(tmp_path)
    h.broker.register_trade(_plan())
    market = OpenTrade("m", "trade-ng-1", 0, "signal", 1)
    assert h.broker.submit(market, NOW).reason == "limit-entry-required"
    h.api.band = (D("2.9"), D("3.5"))
    answer = h.broker.submit(OpenTrade("e", "trade-ng-1", 0, "signal", 1, EntryOrderType.LIMIT, D("2.845")), NOW)
    assert answer.reason == "price-out-of-band" and h.api.posts() == []


def test_lost_answer_found_by_key_after_restart(tmp_path):
    h = Harness(tmp_path)
    h.api.failures.append("accepted-lost")
    action, answer = h.open()
    assert answer.status is ExecutionStatus.ACK and answer.reason == "broker-unknown"
    assert h.kinds() == ["sent", "unknown"]
    # Новый процесс: тот же журнал заявок, поиск по ключу до первого тика.
    restarted = TInvestBroker(h.api, OrderLedger(tmp_path / "broker.sqlite3"), contour="sandbox",
                              publish=h.published.append, clock=h.clock)
    restarted.set_contracts({NG: NG_META})
    restarted.register_trade(_plan())
    restarted.recover(h.clock())
    assert len(h.api.posts()) == 1 and h.kinds()[-1] == "found"
    assert restarted.submit(action, h.clock()).status is ExecutionStatus.ACK and len(h.api.posts()) == 1


def test_not_found_resent_with_same_key_or_cancelled_when_stale(tmp_path):
    h = Harness(tmp_path)
    h.api.failures.append(OrderFailure("unknown", "UNAVAILABLE"))
    action, _ = h.open()
    h.broker.service(h.clock.advance(5))
    keys = [call[2] for call in h.api.posts()]
    assert keys == [order_key(action.command_id)] * 2 and h.kinds()[-1] == "resent"

    other = Harness(tmp_path / "stale")
    other.api.failures.append(OrderFailure("unknown", "UNAVAILABLE"))
    other.open()
    other.broker.service(other.clock.advance(3601))
    events = other.drain()
    assert [(e.status, e.reason) for e in events] == [(ExecutionStatus.CANCEL, "entry-undelivered")]
    assert len(other.api.posts()) == 1


def test_duplicate_key_is_looked_up(tmp_path):
    h = Harness(tmp_path)
    action, _ = h.open()
    h.ledger.update(order_key(action.command_id), state="unknown")
    h.broker.service(h.clock.advance(1))
    assert h.ledger.get(order_key(action.command_id)).state == "accepted"


@pytest.mark.parametrize("failure,reason", [(OrderFailure("rate_limited", "RESOURCE_EXHAUSTED"), "rate-limited"),
                                            (OrderFailure("rejected", "INVALID_ARGUMENT", "30042"), "broker:30042")])
def test_entry_rejected(tmp_path, failure, reason):
    h = Harness(tmp_path)
    h.api.failures.append(failure)
    _, answer = h.open()
    assert answer.status is ExecutionStatus.REJECT and answer.reason == reason
    assert h.drain() == []


def test_cancel_entry_with_fill_before_confirmation(tmp_path):
    h = Harness(tmp_path)
    action, _ = h.open()
    key = order_key(action.command_id)
    answer = h.broker.submit(CancelEntry("cancel-1", "trade-ng-1", 1, "entry-timeout"), h.clock())
    assert answer.status is ExecutionStatus.ACK and h.ledger.get(key).state == CANCELLING
    h.broker.ingest(h.api.fill(key, 1, "2.845"))
    h.broker.ingest(h.api.cancelled(key))
    events = h.drain()
    assert [(e.status, e.command_id) for e in events] == [
        (ExecutionStatus.PARTIAL, action.command_id), (ExecutionStatus.CANCEL, action.command_id),
        (ExecutionStatus.CANCEL, "cancel-1")]
    assert events[1].reason == "entry-timeout" and h.broker.trade_state("trade-ng-1").position == 1


def test_targets_placed_after_entry_and_filled_by_limit(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.broker.service(h.clock())
    targets = h.api.posts("limit")[1:]
    assert [(call[3], call[4], call[5]) for call in targets] == [("SELL", 1, D("2.912")), ("SELL", 1, D("2.978"))]
    first = targets[0][2]
    h.broker.ingest(h.api.fill(first, 1, "2.912"))
    event = h.drain()[0]
    assert event.command_id.startswith("trade-ng-1:pv:tp:tp-1:") and event.status is ExecutionStatus.FILL
    assert h.broker.trade_state("trade-ng-1").position == 1
    # Брокер снял вторую цель в конце дня — после открытия она ставится заново.
    second = targets[1][2]
    h.broker.ingest(h.api.cancelled(second))
    h.broker.service(h.clock.advance(60))
    assert h.api.posts("limit")[-1][5] == D("2.978") and h.api.posts("limit")[-1][2] != second


def test_stop_limit_requote_and_emergency_market(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.broker.service(h.clock())
    target_keys = [call[2] for call in h.api.posts("limit")[1:]]
    h.broker.track_bar(NOW + timedelta(minutes=5), {NG: (2.83, 2.80, 2.84, 2.81)})
    assert {call[1] for call in h.api.calls if call[0] == "cancel"} >= {h.api.orders[k].broker_order_id
                                                                         for k in target_keys}
    for key in target_keys:
        h.broker.ingest(h.api.cancelled(key))
    h.broker.service(h.clock.advance(1))
    stop = h.api.posts("limit")[-1]
    assert (stop[3], stop[4], stop[5]) == ("SELL", 2, D("2.808"))     # стоп 2.813 − 5 шагов
    # Не исполнился за 15 с — снятие и новый лимит от последней цены.
    h.api.prices[NG] = D("2.790")
    h.broker.service(h.clock.advance(16))
    h.broker.ingest(h.api.cancelled(stop[2]))
    h.broker.service(h.clock.advance(1))
    assert h.api.posts("limit")[-1][5] == D("2.785") and "requoted" in h.kinds()
    for _ in range(2):
        last = h.api.posts()[-1][2]
        h.broker.service(h.clock.advance(16))
        h.broker.ingest(h.api.cancelled(last))
        h.broker.service(h.clock.advance(1))
    market = h.api.posts("market")
    assert len(market) == 1 and market[0][4] == 2 and "emergency_market" in h.kinds()
    h.broker.ingest(h.api.fill(market[0][2], 2, "2.780"))
    event = h.drain()[0]
    assert ":pv:stop:" in event.command_id and event.reference_price == D("2.813")
    assert h.broker.trade_state("trade-ng-1").position == 0 and not h.broker.busy()


def test_signal_close_cancels_targets_and_exits_by_limit(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.broker.service(h.clock())
    answer = h.broker.submit(CloseTrade("close-1", "trade-ng-1", 2, "signal-exit"), h.clock())
    assert answer.status is ExecutionStatus.ACK
    for key in [call[2] for call in h.api.posts("limit")[1:3]]:
        h.broker.ingest(h.api.cancelled(key))
    h.broker.service(h.clock.advance(1))
    exit_post = h.api.posts("limit")[-1]
    assert (exit_post[3], exit_post[4], exit_post[5]) == ("SELL", 2, D("2.845"))   # 2.850 − 5 шагов
    h.broker.ingest(h.api.fill(exit_post[2], 2, "2.846"))
    events = h.drain()
    assert [(e.command_id, e.status) for e in events] == [("close-1", ExecutionStatus.FILL)]


def test_reduce_uses_free_lots_and_move_stop(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    stop = h.broker.submit(MoveStop("move-1", "trade-ng-1", 1, "breakeven", D("2.846")), h.clock())
    assert stop.status is ExecutionStatus.ACK and stop.reason == "breakeven"
    assert h.broker.trade_state("trade-ng-1").confirmed_stop == D("2.846")
    h.broker.submit(ReduceTrade("reduce-1", "trade-ng-1", 2, "partial", 1), h.clock())
    assert h.api.posts("limit")[-1][4] == 1
    assert h.broker.submit(ReduceTrade("t", "trade-ng-1", 2, "x", 1, target_id="tp-1"), NOW).reason == \
        "targets-at-broker"


def test_exit_rejected_alarm_once_and_retry_after_minute(tmp_path):
    h = Harness(tmp_path)
    h.filled()
    h.api.failures.append(OrderFailure("rejected", "FAILED_PRECONDITION", "30079", "торги закрыты"))
    h.broker.submit(CloseTrade("close-1", "trade-ng-1", 2, "session"), h.clock())
    assert h.kinds().count("exit_rejected") == 1
    posts = len(h.api.posts())
    h.broker.service(h.clock.advance(30))
    assert len(h.api.posts()) == posts
    h.broker.service(h.clock.advance(31))
    assert len(h.api.posts()) == posts + 1 and h.kinds().count("exit_rejected") == 1


def test_consumed_execution_not_emitted_again(tmp_path):
    h = Harness(tmp_path)
    key = h.filled()
    assert h.ledger.pending_executions() and h.drain() == []
    assert h.ledger.pending_executions() == []
    assert h.ledger.get(key).state == "filled"


def test_margin_from_futures_margin_replaces_core_card(tmp_path):
    h = Harness(tmp_path)
    assert h.broker.refresh_margins(NOW) == [NG]
    assert h.broker.contract_for(NG).go_buy == pytest.approx(6228.44)
    assert h.broker.refresh_margins(NOW + timedelta(hours=1)) == []


def test_sell_side_entry_and_cancel_without_live_orders(tmp_path):
    h = Harness(tmp_path)
    h.broker.register_trade(_plan("short-1", side="SELL"))
    answer = h.broker.submit(OpenTrade("s", "short-1", 0, "signal", 1, EntryOrderType.LIMIT, D("2.8451")), NOW)
    assert answer.status is ExecutionStatus.ACK and h.api.posts()[-1][5] == D("2.846")
    h.broker.ingest(h.api.cancelled(order_key("s")))
    assert [e.reason for e in h.drain()] == ["broker-cancelled"]
    assert h.ledger.get(order_key("s")).state == CANCELLED
    assert h.broker.submit(CancelEntry("c", "short-1", 1, "entry-timeout"), NOW).status is ExecutionStatus.CANCEL


def test_broker_has_everything_trade_manager_and_bar_loop_call(tmp_path):
    """Сборка: методы, которые зовут TradeManager (через getattr тоже) и исполнение по барам контура."""
    broker = Harness(tmp_path).broker
    for name in ("submit", "register_trade", "resume_trade", "trade_state", "has_recovery_guard_close",
                 "contract_for", "set_contracts", "set_names", "track_bar", "drain_addressed_events", "drain_events"):
        assert callable(getattr(broker, name)), name


def test_trading_bot_strategy_cache_grows_with_added_binding():
    from src.bot.trading_bot import TradingBot
    from src.strategies.contracts import Assignment

    future: dict = {}
    built = []
    bot = TradingBot(instruments=[], bus=None, strategy_map={"macd_rsi_stoch": {}}, data_cache=None, timeline=None,
                     strategy_factory=lambda name, config: built.append(name) or object(),
                     share_strategies={}, future_strategies=future)
    assert bot._strategy_cache == {}
    future["NG"] = [Assignment("future-ng-macd", "macd_rsi_stoch", "levels_rr", timeframe="15m")]
    assert set(bot._strategy_cache) == {("macd_rsi_stoch", "15m")} and built == ["macd_rsi_stoch"]
    assert bot._strategy_cache and built == ["macd_rsi_stoch"], "уже построенная стратегия не пересоздаётся"
