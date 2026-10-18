"""Предзагрузка расписания исторического прогона и предупреждение (add-reconstructed-history-schedule D6)."""

import copy
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.history.schedule_notice import preload_history_schedule, schedule_ranges
from src.market_calendar.service import EmulatorScheduleSource, MarketCalendar, SnapshotScheduleStore
from src.strategies.contracts import Assignment
from src.trade_management.intraday_settings import IntradaySettings
from tests.unit.market_calendar.test_reconstructed_schedule import fixture_days

TEMPLATE = fixture_days()[1]  # пятница 2025-04-18, восстановленный торговый день


def synthetic_day(value: date) -> dict:
    day = copy.deepcopy(TEMPLATE)
    day.pop("version")  # версия сверяется, только если заявлена
    text = json.dumps(day["day"]).replace("2025-04-18", value.isoformat())
    return {**day, "trading_date": value.isoformat(), "day": json.loads(text)}


class Provider:
    """Эмулятор: восстановленные дни на все даты, кроме ``holes``; записывает запросы."""

    snapshot_generation = 7

    def __init__(self, *, holes=(), error=None):
        self.holes, self.error, self.calls = set(holes), error, []

    @contextmanager
    def client_context(self, token=None):
        def trading_schedules(*, instrument_id, from_, to):
            self.calls.append((from_.date(), to.date()))
            if self.error is not None:
                raise self.error
            value, days = from_.date(), []
            while value <= to.date():
                if value not in self.holes:
                    days.append(synthetic_day(value))
                value += timedelta(days=1)
            return {"instrument_id": instrument_id, "exchange": "forts_futures_weekend",
                    "binding_provenance": "reconstructed", "snapshot_generation": 7,
                    "days": days, "missing_dates": []}
        yield SimpleNamespace(trading_schedules=trading_schedules)


def calendar(provider, *, allow=True):
    store = SnapshotScheduleStore()
    return MarketCalendar(store, EmulatorScheduleSource(store, provider), allow_reconstructed=allow)


BR = SimpleNamespace(uid="br-uid", ticker="BRJ5", instrument_type="future", family="BR", short_name="BR-4.25")
STRATEGIES = {"BR": [
    Assignment("future-br-macd", "macd_rsi_stoch", "levels_rr", timeframe="5m"),
    Assignment("future-br-harmonic", "harmonic", "pattern_targets", timeframe="1h"),
]}
START, END = datetime(2025, 3, 10, 9), datetime(2025, 4, 10, 9)


def ranges(strategies=STRATEGIES, start=START, end=END):
    return schedule_ranges(None, [BR], start, end, IntradaySettings(), {}, strategies)


def test_range_lists_only_intraday_assignments():
    (item,) = ranges()

    assert (item.instrument_uid, item.first, item.last) == ("br-uid", date(2025, 3, 10), date(2025, 4, 10))
    assert item.intraday == ("future-br-macd (5m)",)


def test_chain_segments_give_their_own_ranges():
    card = SimpleNamespace(uid="br-uid", instrument=BR)  # тот же контракт и в selection.instruments
    segment = SimpleNamespace(card=card, start=datetime(2025, 3, 30, 16, 5), end=datetime(2025, 4, 30, 16, 5))
    selection = SimpleNamespace(chains=((segment,),))

    (item,) = schedule_ranges(selection, [BR], START, END, IntradaySettings(), {}, STRATEGIES)

    assert (item.instrument_uid, item.first, item.last) == ("br-uid", date(2025, 3, 30), date(2025, 4, 30))


def test_long_range_is_loaded_in_chunks_and_ticks_do_not_request():
    provider = Provider()
    market = calendar(provider)
    item = ranges(end=datetime(2026, 6, 1))

    notice = preload_history_schedule(market, item, START, IntradaySettings())

    assert len(provider.calls) == 2
    assert all((to - first).days < 366 for first, to in provider.calls)
    assert notice.level == "info" and "восстановлено — " in notice.text
    provider.calls.clear()
    assert market.calendar_for("br-uid", datetime(2025, 5, 5, 9), include_reconstructed=True) is not None
    assert not market.service(datetime(2025, 5, 5, 9))
    assert provider.calls == []


def test_missing_dates_warn_with_intraday_assignments():
    holes = {date(2025, 3, 11), date(2025, 3, 12), date(2025, 4, 1)}
    market = calendar(Provider(holes=holes))

    notice = preload_history_schedule(market, ranges(), START, IntradaySettings())

    assert notice.level == "warning"
    assert "3 даты без расписания (2025-03-11 — 2025-04-01)" in notice.text
    assert "future-br-macd (5m)" in notice.text and "harmonic" not in notice.text


def test_disabled_reconstruction_warns_about_every_date():
    market = calendar(Provider(), allow=False)

    notice = preload_history_schedule(market, ranges(), START, IntradaySettings())

    assert notice.level == "warning" and "32 даты без расписания" in notice.text


@pytest.mark.parametrize("settings, strategies", [
    (IntradaySettings(enabled=False), STRATEGIES),
    (IntradaySettings(), {"BR": [STRATEGIES["BR"][1]]}),
])
def test_no_warning_without_guard_or_intraday_assignments(settings, strategies):
    market = calendar(Provider(holes={date(2025, 3, 11)}))
    items = schedule_ranges(None, [BR], START, END, settings, {}, strategies)

    notice = preload_history_schedule(market, items, START, settings)

    assert notice is None or notice.level == "info"


def test_emulator_error_does_not_stop_run_and_dates_stay_lazy():
    provider = Provider(error=RuntimeError("down"))
    market = calendar(provider)

    notice = preload_history_schedule(market, ranges(), START, IntradaySettings())

    assert notice.level == "warning"
    provider.error = None
    provider.calls.clear()
    market.calendar_for("br-uid", datetime(2025, 3, 20, 9), include_reconstructed=True)
    assert market.service(datetime(2025, 3, 20, 9))
    assert provider.calls  # ленивая загрузка тиком
