"""Потребности торговых контуров в рыночной базе (add-contour-process, D3).

Процесс контура пишет под своим ``contour_id`` спрос пар (``contour_demand``) и
обязательства незавершённых сделок (``contour_obligations``). Ядро опрашивает
обе таблицы и применяет их так же, как прежние вызовы внутри процесса:
спрос — запросом прогрева истории, обязательства — границей исполнения
владельца входа. Модуль не импортирует контур.
"""

from __future__ import annotations

import threading
import time

from src.assembly.contracts import instrument_from_payload
from src.instruments.model import instrument_key
from src.logging_setup import get_logger

log = get_logger(__name__)


def merge_obligations(by_contour) -> dict:
    """Обязательства всех контуров на UID: самая ранняя граница, при равной — включительная."""
    result: dict = {}
    for obligations in by_contour.values():
        for key, (stamp, inclusive) in obligations.items():
            previous = result.get(key)
            if previous is None or stamp < previous[0]:
                result[key] = (stamp, inclusive)
            elif stamp == previous[0]:
                result[key] = (stamp, inclusive or previous[1])
    return result


def merge_demand(rows) -> dict:
    """Спрос всех контуров: ``{(ключ, ТФ): (инструмент, глубина)}`` по наибольшей глубине."""
    result: dict = {}
    for row in rows:
        key = (row["instrument_key"], row["interval"])
        previous = result.get(key)
        if previous is None or row["min_bars"] > previous[1]:
            result[key] = (instrument_from_payload(row["instrument"]), row["min_bars"])
    return result


class ContourDemandPoller:
    """Поток ядра: раз в ``poll_seconds`` применяет потребности контуров из рыночной базы."""

    def __init__(self, market_store, data_cache, *, worker=None, instruments=(), tracks_obligations: bool,
                 poll_seconds: float = 1.0, release_seconds: float = 900.0, on_release=None,
                 clock=time.monotonic) -> None:
        self._store = market_store
        self._cache = data_cache
        self._worker = worker
        self._instruments = list(instruments)
        self._tracks = tracks_obligations
        self._poll = poll_seconds
        self._applied: dict = {}
        # Снятие пар без спроса (add-contour-membership): ключ → (инструмент, момент пропажи спроса).
        self._instruments_by_key: dict = {}
        self._missing_since: dict = {}
        self._release = release_seconds
        self._on_release = on_release
        self._clock = clock
        self._obligations: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add_instruments(self, instruments) -> None:
        """Инструменты повторного запуска контура: их границы исполнения тоже применяются."""
        known = {instrument_key(item) for item in self._instruments}
        self._instruments = self._instruments + [item for item in instruments if instrument_key(item) not in known]
        self._obligations = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="contour-demand", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # опрос не должен останавливать приём данных
                log.warning("Потребности контуров не применены (%s)", type(exc).__name__, exc_info=True)
            self._stop.wait(self._poll)

    def poll_once(self) -> None:
        demand = merge_demand(self._store.contour_demands())
        for key, (instrument, min_bars) in demand.items():
            self._instruments_by_key[key] = instrument
            self._missing_since.pop(key, None)
            if self._applied.get(key, 0) >= min_bars:
                continue
            # Как прежний вызов кэша ядра: пара регистрируется и прогревается до глубины.
            self._cache.request_live_history(instrument, key[1], min_bars)
            self._applied[key] = min_bars
        if self._tracks:
            self.apply_obligations()
        self._release_unneeded(demand)

    def _release_unneeded(self, demand) -> None:
        """Пара без спроса контуров и без обязательств снимается после выдержки ``release_seconds``."""
        now = self._clock()
        held = self.obligations() if self._tracks else {}
        for key in [key for key in self._applied if key not in demand]:
            instrument = self._instruments_by_key[key]
            uid = self._pair_uid(instrument, key[1])
            if key[0] in held or (uid and uid in held):
                self._missing_since.pop(key, None)
                continue
            since = self._missing_since.setdefault(key, now)
            if now - since < self._release:
                continue
            self._applied.pop(key, None)
            self._missing_since.pop(key, None)
            if uid and self._cache.unregister_live_pair(uid, key[1]):
                if self._worker is not None:
                    self._worker.retire_roster_pairs([(uid, key[1])])
                log.info("Пара %s %s не нужна ни одному контуру — снята с обслуживания",
                         instrument.short_name or instrument.ticker, key[1])
            if self._on_release is not None:
                self._on_release(instrument, key[1])

    def _pair_uid(self, instrument, timeframe):
        pair_uid = getattr(self._cache, "pair_uid", None)
        return pair_uid(instrument, timeframe) if callable(pair_uid) else getattr(instrument, "uid", None)

    def obligations(self) -> dict:
        return merge_obligations(self._store.contour_obligations())

    def apply_obligations(self) -> None:
        merged = self.obligations()
        if merged == self._obligations:
            return
        self._obligations = merged
        if self._worker is not None:
            self._worker.set_execution_obligations(merged)
        for instrument in self._instruments:
            boundary = merged.get(instrument_key(instrument))
            if boundary is None:
                # Live-выбор строит инструменты без UID: ключ обязательства — UID пары.
                pair_uid = getattr(self._cache, "pair_uid", None)
                uid = pair_uid(instrument, "1m") if callable(pair_uid) else getattr(instrument, "uid", None)
                boundary = merged.get(uid) if uid else None
            if boundary is not None:
                self._cache.note_execution_lower(instrument, boundary[0])
