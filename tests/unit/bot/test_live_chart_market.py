"""Графики переноса стопа и итога в обоих циклах робота (live-chart-market).

15.10.2026 перенос стопа и закрытие BR-11.26 ушли в Telegram текстом: live-цикл
не передавал менеджеру свечи пары, и снимок исполнения оставался без графика.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from src.broker.port import ExecutionEvent, ExecutionStatus
from src.events.event import Event
from src.events.types import EventType
from src.portfolio.models import ContractMeta
from src.trade_journal.storage import Storage
from src.trade_management.actions import MoveStop, OpenTrade, ReduceTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import CostSnapshot, PlanEconomics, ProfileSnapshot, TargetPlan, TradePlan

from tests.unit.bot.test_management_timeframes import _live, _trade
from tests.unit.bot.test_trading_bot import FakeCache, FakeTimeline, RecordingChannel, _df, _inst, _make_bot, _make_strategy
from tests.unit.notifier.test_visual_delivery import channel, server  # noqa: F401 — фикстура pytest
from tests.unit.test_live_analysis import frame

D = Decimal
PNG = b"\x89PNG\r\n\x1a\n"


def _remembered(manager):
    return [(call.args[1], str(call.args[2]["datetime"].iloc[-1])) for call in manager.remember_market.call_args_list]


def _new_bars(live, data, clock):
    data["SBER", "1h"] = frame("2026-10-12 09:00")
    data["SBER", "15m"] = frame("2026-10-12 09:45", "15m")
    data["SBER", "1m"] = frame("2026-10-12 09:59", "1m", count=300)
    clock[0] += timedelta(seconds=5)
    live.worker.run_until_idle()


def test_live_step_remembers_chart_candles_of_bound_and_trade_timeframes_once_per_bar():
    manager = Mock()
    manager.management_obligations.return_value = (_trade("1h", ma_slow_period=40),)
    manager.manage.return_value = ()
    manager.actions_for_signal.return_value = SimpleNamespace(actions=(), rejections=())
    live, data, events, clock = _live(manager)
    live.bootstrap()
    _new_bars(live, data, clock)

    live.step()

    # Привязка на 15m и ТФ сделки без привязки (1h); служебный 1m графику не нужен.
    remembered = _remembered(manager)
    assert ("15m", "2026-10-12 09:45:00") in remembered
    assert ("1h", "2026-10-12 09:00:00") in remembered
    assert not any(tf == "1m" for tf, _ in remembered)
    hourly = next(call for call in manager.remember_market.call_args_list if call.args[1] == "1h")
    assert hourly.args[3] == datetime(2026, 10, 12, 10, 0)
    assert "breaks" in hourly.kwargs

    # Опрос без новых свечей не пересчитывает снимок и разрывы.
    live.step()
    assert _remembered(manager) == remembered


def test_tick_cycle_remembers_trade_timeframe_without_binding_but_not_protection_1m():
    trade_manager = MagicMock()
    trade_manager.manage.return_value = ()
    trade_manager.management_obligations.return_value = (_trade("1h", ma_slow_period=40),)
    bot = _make_bot(
        timeline=FakeTimeline(timeframes=("1m", "1h")),
        cache=FakeCache(frames={("SBER", "1m"): _df(), ("SBER", "1h"): _df()}),
        bus=RecordingChannel().bus, strategy=_make_strategy(),
        share={}, trade_manager=trade_manager,
    )
    bot._instruments = [_inst("SBER", "SBER", "share")]

    bot.run()

    assert [call.args[1] for call in trade_manager.remember_market.call_args_list] == ["1h"]


class Broker:
    def contract_for(self, ticker):
        return ContractMeta(ticker, .01, .1, 0, 0)


def _open_trade(manager, now):
    plan = TradePlan("trade", "SBER-15m", "SBER", "BUY", "signal", D(300), D(297),
                     (TargetPlan("first", D("303.4"), D(".5")), TargetPlan("second", D("306.4"), D(".5"))),
                     ProfileSnapshot("levels_rr", "1", {}), now, timeframe="15m", algorithm_version="economics-v2",
                     cost_snapshot=CostSnapshot(), entry_order_type="limit", price_step=D(".01"),
                     economics=PlanEconomics(2, D(60), D(98), D(8), D("1.5"), target_quantities={"first": 1, "second": 1}))
    entry = OpenTrade("entry", "trade", 0, "entry", 2, "limit", D(300))
    assert manager.submit_plan(plan, entry, price_step=D(".01"), step_cost=D(".1"))
    return entry


def _fact(manager, action, key, status, now, quantity=0, price=None, minute=0, reason="confirmed"):
    event = ExecutionEvent(key, action.command_id, action.command_id, action.trade_id, status, quantity,
                           None if price is None else D(str(price)), None if price is None else D("1.5"),
                           now + timedelta(minutes=minute), reason)
    assert manager.consume(event)


def test_live_stop_move_and_close_are_sent_as_charts_with_exit_markers(tmp_path, server):  # noqa: F811
    now = datetime(2026, 10, 12, 10, 30)
    facts = []
    with Storage(tmp_path / "trade.db") as storage:
        manager = TradeManager(storage, Broker(), initial_balance=D(100000), max_qty=10,
                               execution_observer=lambda event, data: facts.append(data), clock=lambda: now)
        live, data, events, clock = _live(manager)
        live.bootstrap()
        _new_bars(live, data, clock)
        live.step()  # live-цикл, не тиковый, передаёт свечи менеджеру

        entry = _open_trade(manager, now)
        _fact(manager, entry, "entry-fill", ExecutionStatus.FILL, now, 2, 300)
        target = ReduceTrade("target", "trade", storage.load_trade("trade").state.state_revision, "target", 1, "first")
        manager.submit_action(target)
        _fact(manager, target, "tp1", ExecutionStatus.FILL, now, 1, "303.4", minute=5)
        move = MoveStop("move", "trade", storage.load_trade("trade").state.state_revision,
                        "cost-aware-break-even", D("297.25"))
        manager.submit_action(move)
        _fact(manager, move, "accepted", ExecutionStatus.ACK, now, minute=6, reason="next-bar")
        _fact(manager, move, "stop-active", ExecutionStatus.ACK, now, minute=7)
        stop_visual = facts[-1]["visual"]
        target2 = ReduceTrade("target2", "trade", storage.load_trade("trade").state.state_revision, "target", 1, "second")
        manager.submit_action(target2)
        _fact(manager, target2, "tp2", ExecutionStatus.FILL, now, 1, "306.4", minute=10)
        final_visual = facts[-1]["visual"]

    assert stop_visual.to_dict()["market"]["candles"] and final_visual.to_dict()["market"]["candles"]
    assert final_visual.to_dict()["state"]["phase"] == "CLOSED"
    instance = channel(tmp_path / "delivery.db")
    for event_type, visual in ((EventType.STOP_MOVED, stop_visual), (EventType.TARGET_HIT, final_visual)):
        instance.handle(Event.broker_event(event_type, trade_id="trade", instrument="SBER", timeframe="15m",
                                           bar_time=datetime.fromisoformat(visual.to_dict()["as_of"]), status="fill", visual=visual))
    instance.close(10)

    posts = [call for call in server.calls if call["method"] in {"sendPhoto", "sendMessage"}]
    assert [call["method"] for call in posts] == ["sendPhoto", "sendPhoto"]
    assert all(call["photo"].startswith(PNG) for call in posts)
    assert "серый пунктир" in posts[0]["data"]["caption"]

    from src.notifier.telegram_chart import build_scene

    stop_scene = build_scene(Event.broker_event(EventType.STOP_MOVED, trade_id="trade", visual=stop_visual))
    # Ступени: исходный стоп, защита входа, подтверждённый перенос.
    assert [step["price"] for step in stop_scene["stop_steps"]] == ["297", "297", "297.25"]
    final_scene = build_scene(Event.broker_event(EventType.TARGET_HIT, trade_id="trade", visual=final_visual))
    assert [marker["label"].split(" · ")[0] for marker in final_scene["markers"]] == ["Вход", "ЦЕЛЬ1", "ЦЕЛЬ2"]
    assert final_scene["result"].startswith("ИТОГ")


def test_chart_failure_is_logged_by_type_and_does_not_stop_management(caplog):
    trade_manager = MagicMock()
    trade_manager.manage.return_value = ()
    trade_manager.management_obligations.return_value = (_trade("1h", ma_slow_period=40),)
    trade_manager.remember_market.side_effect = ValueError("https://proxy.test/bot123:secret")
    bot = _make_bot(
        timeline=FakeTimeline(timeframes=("1m", "1h")),
        cache=FakeCache(frames={("SBER", "1m"): _df(), ("SBER", "1h"): _df()}),
        bus=RecordingChannel().bus, strategy=_make_strategy(),
        share={}, trade_manager=trade_manager,
    )
    bot._instruments = [_inst("SBER", "SBER", "share")]

    bot.run()

    assert [call.kwargs["timeframe"] for call in trade_manager.manage.call_args_list] == ["1m", "1h"]
    assert "1h не переданы (ValueError)" in caplog.text
    assert "secret" not in caplog.text
    # Неудачный кадр не помечен переданным: следующий обход повторит попытку.
    assert bot._remembered_markets == {}
