"""Постановка закрытых баров воркеру паттернов ядром: адрес пары — UID рыночного слоя.

С add-contour-market-feed бар ставит сторож ядра (``PatternTrigger``), а не
торговый тик контура.
"""

from datetime import datetime, timedelta

import pandas as pd

from src.instruments import normalize_instrument
from src.market_structure.trigger import PatternTrigger

PHOR_UID = "9978b56f-782a-4a80-a4b1-a48cbecfd194"


class Grid:
    def grid(self, timeframe):
        return self

    def bar_close(self, bar_start):
        return bar_start + timedelta(minutes=15)

    def now(self):
        return datetime(2026, 10, 14, 7, 0)


class PairCache:
    """Рыночный слой: UID известен по регистрации пары, а не по карточке инструмента."""

    def __init__(self, uids=None):
        self.uids = dict(uids or {})
        self.bars = {}

    def pair_uid(self, instrument, timeframe):
        return self.uids.get((instrument.ticker, timeframe))

    def pattern_bar(self, instrument, timeframe):
        return self.bars.get((instrument.ticker, timeframe), (None, False))

    def close(self, instrument, timeframe, stamp, ready=True):
        self.bars[instrument.ticker, timeframe] = (pd.Timestamp(stamp), ready)


class Worker:
    def __init__(self):
        self.tasks, self.primed = [], []

    def submit(self, **task):
        self.tasks.append(task)
        return True

    def prime(self, **mark):
        self.primed.append(mark)


PHOR = normalize_instrument(("PHOR", "PHOR", "share"))


def trigger(cache, worker, *pairs):
    return PatternTrigger(cache, Grid(), worker, pairs or [(PHOR, "15m")])


def test_instrument_selected_without_card_uid_gets_task_under_pair_uid():
    assert PHOR.uid is None                                  # live-выбор не знает UID
    cache, worker = PairCache({("PHOR", "15m"): PHOR_UID}), Worker()
    watch = trigger(cache, worker)
    cache.close(PHOR, "15m", "2026-10-14 06:30")
    watch.observe()
    cache.close(PHOR, "15m", "2026-10-14 06:45")
    watch.observe()

    assert worker.tasks == [{"ticker": "PHOR", "instrument_uid": PHOR_UID, "timeframe": "15m",
                             "close_time": pd.Timestamp("2026-10-14 07:00")}]


def test_startup_mark_uses_pair_uid_once_pair_is_warm():
    cache, worker = PairCache({("PHOR", "15m"): PHOR_UID}), Worker()
    watch = trigger(cache, worker)
    # Прогрев ещё идёт: бары догружаемой истории — не повод ни для отметки, ни для задач.
    cache.close(PHOR, "15m", "2026-10-13 18:30", ready=False)
    watch.observe()
    cache.close(PHOR, "15m", "2026-10-14 06:45", ready=True)
    watch.observe()

    assert worker.primed == [{"instrument_uid": PHOR_UID, "timeframe": "15m",
                              "close_time": pd.Timestamp("2026-10-14 07:00")}]
    assert worker.tasks == []


def test_unknown_pair_uid_warns_once_and_keeps_going(caplog):
    ng = normalize_instrument(("NG-11.26", "NGX6", "future", "NG-11.26"))
    cache, worker = PairCache(), Worker()
    watch = trigger(cache, worker, (ng, "15m"))

    with caplog.at_level("WARNING", logger="src.market_structure.trigger"):
        for stamp in ("2026-10-14 06:30", "2026-10-14 06:45", "2026-10-14 07:00"):
            cache.close(ng, "15m", stamp)
            watch.observe()

    assert worker.tasks == [] and worker.primed == []
    warnings = [record.getMessage() for record in caplog.records if "UID пары" in record.getMessage()]
    assert warnings == ["Паттерны: UID пары NG-11.26 15m неизвестен — закрытые бары не анализируются"]
    assert "NGX6" not in warnings[0]                        # пользователю — короткое имя контракта


def test_bar_after_uid_appears_is_submitted_and_warning_rearms(caplog):
    cache, worker = PairCache(), Worker()
    watch = trigger(cache, worker)

    with caplog.at_level("WARNING", logger="src.market_structure.trigger"):
        cache.close(PHOR, "15m", "2026-10-14 06:30")
        watch.observe()
        cache.uids[("PHOR", "15m")] = PHOR_UID
        watch.observe()                                      # стартовая отметка 06:45
        cache.close(PHOR, "15m", "2026-10-14 06:45")
        watch.observe()
        del cache.uids[("PHOR", "15m")]
        cache.close(PHOR, "15m", "2026-10-14 07:00")
        watch.observe()

    assert [task["close_time"] for task in worker.tasks] == [pd.Timestamp("2026-10-14 07:00")]
    assert sum("UID пары" in record.getMessage() for record in caplog.records) == 2


def test_repeated_pass_on_same_bar_creates_one_task():
    from src.market_structure.worker import PatternWorker

    cache = PairCache({("PHOR", "15m"): PHOR_UID})
    worker = PatternWorker(store=None, redis_client=None)       # поток не запускается
    watch = trigger(cache, worker)
    cache.close(PHOR, "15m", "2026-10-14 06:30")
    watch.observe()
    cache.close(PHOR, "15m", "2026-10-14 06:45")
    for _ in range(3):
        watch.observe()

    assert worker._queue.qsize() == 1


def test_pair_outside_demand_gets_no_task():
    sber = normalize_instrument(("SBER", "SBER", "share"))
    cache, worker = PairCache({("PHOR", "15m"): PHOR_UID, ("SBER", "15m"): "uid-sber"}), Worker()
    watch = trigger(cache, worker)
    cache.close(sber, "15m", "2026-10-14 06:30")
    watch.observe()
    cache.close(sber, "15m", "2026-10-14 06:45")
    watch.observe()

    assert worker.tasks == [] and worker.primed == []


def test_trading_bot_no_longer_talks_to_pattern_worker():
    import inspect

    from src.bot import TradingBot

    assert "pattern_worker" not in inspect.signature(TradingBot).parameters
