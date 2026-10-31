"""Сигнал по контракту с обязательствами ждёт исполнения минуты (fix-signal-awaits-execution)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.instruments.model import instrument_key
from src.trade_management.models import SignalAdmission
from tests.unit.test_live_analysis import frame, setup


def live_with_position(*, obligations):
    manager = Mock()
    manager.management_obligations.return_value = ()
    manager.manage.return_value = ()
    manager.actions_for_signal.return_value = SignalAdmission()
    live, cache, data, events, clock = setup(now=datetime(2026, 10, 12, 10, 1), end="2026-10-12 06:00",
                                             cutover=datetime(2026, 10, 12, 6, 0), manager=manager)
    sber = live.bot._instruments[0]
    manager.contract_has_obligations.side_effect = lambda key: obligations and key == instrument_key(sber)
    live.bootstrap()
    # Бар 1h 09:00–10:00 и его последняя минута 09:59 получены, исполнение дошло до 09:58.
    data["SBER", "1h"] = frame("2026-10-12 09:00")
    data["SBER", "1m"] = frame("2026-10-12 09:59", "1m", count=300)
    clock[0] += timedelta(seconds=5)
    live.worker.run_until_idle()
    execute_through(cache, "2026-10-12 09:58")
    return live, cache, manager


def execute_through(cache, minute):
    cache._market_store.mark_processed(source=cache._market_source, instrument_uid="SBER", interval="1m",
                                       open_time=minute, contour=cache.contour_id)


def live_admissions(manager):
    return [call for call in manager.actions_for_signal.call_args_list if call.args[0].id == "SBER-1h"
            and call.args[1].bar_time.hour == 10]


def test_received_but_unexecuted_last_minute_holds_signal_until_execution():
    live, cache, manager = live_with_position(obligations=True)
    live.step()
    # Минута 09:59 получена, но не исполнена: бар ждёт, допуск не вызывается.
    assert live_admissions(manager) == []
    assert live.states["SBER", "1h"].hold == {"kind": "execution_barrier",
                                              "through": datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)}
    execute_through(cache, "2026-10-12 09:59")
    live.step()
    assert len(live_admissions(manager)) == 1
    assert live.states["SBER", "1h"].hold is None


def test_without_obligations_signal_does_not_wait_for_execution():
    live, cache, manager = live_with_position(obligations=False)
    live.step()
    assert len(live_admissions(manager)) == 1


def test_entry_market_execution_flag_uses_executed_minute():
    live, cache, _ = live_with_position(obligations=True)
    sber = live.bot._instruments[0]
    assert cache.execution_processed(sber, datetime(2026, 10, 12, 9, 58))
    assert not cache.execution_processed(sber, datetime(2026, 10, 12, 9, 59))
