"""Постановка закрытых баров воркеру паттернов: адрес пары — UID рыночного слоя."""

from datetime import datetime, timedelta

import pandas as pd

from src.bot import TradingBot
from src.events import EventBus
from src.instruments import normalize_instrument

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

    def pair_uid(self, instrument, timeframe):
        return self.uids.get((instrument.ticker, timeframe))


class Worker:
    def __init__(self):
        self.tasks, self.primed = [], []

    def submit(self, **task):
        self.tasks.append(task)
        return True

    def prime(self, **mark):
        self.primed.append(mark)


def frame(*stamps):
    stamps = [pd.Timestamp(item) for item in stamps]
    return pd.DataFrame({"datetime": stamps, "open": [1.0] * len(stamps), "high": [1.0] * len(stamps),
                         "low": [1.0] * len(stamps), "close": [1.0] * len(stamps)})


def bot(cache, worker):
    return TradingBot(instruments=[], bus=EventBus(), strategy_map={}, data_cache=cache, timeline=Grid(),
                      pattern_worker=worker)


def test_instrument_selected_without_card_uid_gets_task_under_pair_uid():
    phor = normalize_instrument(("PHOR", "PHOR", "share"))
    assert phor.uid is None                                  # live-выбор не знает UID
    worker = Worker()
    robot = bot(PairCache({("PHOR", "15m"): PHOR_UID}), worker)

    robot._submit_pattern_bar(phor, "15m", frame("2026-10-14 06:30", "2026-10-14 06:45"))

    assert worker.tasks == [{"ticker": "PHOR", "instrument_uid": PHOR_UID, "timeframe": "15m",
                             "close_time": pd.Timestamp("2026-10-14 07:00")}]


def test_startup_mark_uses_pair_uid():
    phor = normalize_instrument(("PHOR", "PHOR", "share"))
    worker = Worker()
    robot = bot(PairCache({("PHOR", "15m"): PHOR_UID}), worker)

    robot._prime_pattern_bar(phor, "15m", frame("2026-10-14 06:45"))

    assert worker.primed == [{"instrument_uid": PHOR_UID, "timeframe": "15m",
                              "close_time": pd.Timestamp("2026-10-14 07:00")}]


def test_unknown_pair_uid_warns_once_and_keeps_trading(caplog):
    ng = normalize_instrument(("NG-11.26", "NGX6", "future", "NG-11.26"))
    worker = Worker()
    robot = bot(PairCache(), worker)

    with caplog.at_level("WARNING", logger="src.bot.trading_bot"):
        for stamp in ("2026-10-14 06:30", "2026-10-14 06:45", "2026-10-14 07:00"):
            robot._submit_pattern_bar(ng, "15m", frame(stamp))

    assert worker.tasks == []
    warnings = [record.getMessage() for record in caplog.records if "UID пары" in record.getMessage()]
    assert warnings == ["Паттерны: UID пары NG-11.26 15m неизвестен — закрытые бары не анализируются"]
    assert "NGX6" not in warnings[0]                        # пользователю — короткое имя контракта


def test_bar_after_uid_appears_is_submitted_and_warning_rearms(caplog):
    phor = normalize_instrument(("PHOR", "PHOR", "share"))
    cache, worker = PairCache(), Worker()
    robot = bot(cache, worker)

    with caplog.at_level("WARNING", logger="src.bot.trading_bot"):
        robot._submit_pattern_bar(phor, "15m", frame("2026-10-14 06:30"))
        cache.uids[("PHOR", "15m")] = PHOR_UID
        robot._submit_pattern_bar(phor, "15m", frame("2026-10-14 06:45"))
        del cache.uids[("PHOR", "15m")]
        robot._submit_pattern_bar(phor, "15m", frame("2026-10-14 07:00"))

    assert [task["close_time"] for task in worker.tasks] == [pd.Timestamp("2026-10-14 07:00")]
    assert sum("UID пары" in record.getMessage() for record in caplog.records) == 2


def test_repeated_tick_on_same_bar_relies_on_worker_dedup():
    from src.market_structure.worker import PatternWorker

    phor = normalize_instrument(("PHOR", "PHOR", "share"))
    worker = PatternWorker(store=None, redis_client=None)       # поток не запускается
    robot = bot(PairCache({("PHOR", "15m"): PHOR_UID}), worker)

    for _ in range(3):
        robot._submit_pattern_bar(phor, "15m", frame("2026-10-14 06:45"))

    assert worker._queue.qsize() == 1
