"""Желаемое ядра в торговой части контура на брокере (add-tinvest-orders, design.md решения 2 и 12).

Участник состава — привязка: её стратегия добавляется в словари стратегий
бота, инструмент прогревается и допускается тем же ``_Roster``, что у эмуляции.
Снятая привязка (``no_entries`` / ``close_all``) доводит свои сделки до конца и
уходит; последняя привязка инструмента уводит и инструмент. Остановка всего
контура — ``TradeManager.stop_contour``; процесс завершается, когда сделок и
незавершённых заявок не осталось. Протокол тот же, что у ``ContourControl``:
``start``, ``before_tick``, ``after_tick``, ``close``.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

from src.assembly.contracts import contracts_from_payload, instrument_from_payload
from src.assembly.core_state import read_desired
from src.instruments.model import instrument_key
from src.logging_setup import get_logger

log = get_logger(__name__)

WARMING, TRADING, DRAINING, CLOSING, REJECTED = "warming", "trading", "draining", "closing", "rejected"
RUNNING = "running"


def assignment_index(share_strategies: dict, future_strategies: dict) -> dict:
    """``id`` привязки → (вид, ключ словаря конфигурации, ``Assignment``)."""
    index = {}
    for kind, strategies in (("share", share_strategies), ("future", future_strategies)):
        for key, items in strategies.items():
            for item in items:
                index[item.id] = (kind, key, item)
    return index


class BrokerControl:
    def __init__(self, *, core_db, contour_id: str, index: dict, strategies: tuple[dict, dict],
                 request_stop: Callable[[str], None], status: Callable[..., None],
                 poll: Callable[[], None] = lambda: None, broker_busy: Callable[[], bool] = lambda: False,
                 core_lost: Callable[[], bool] = lambda: False, warm_timeout: float = 600.0,
                 clock: Callable[[], float] = time.monotonic):
        self._core_db = core_db
        self._contour_id = contour_id
        self._index = index
        self._share, self._future = strategies
        self._request_stop = request_stop
        self._status = status
        self._poll = poll
        self._busy = broker_busy
        self._core_lost = core_lost
        self._warm_timeout = warm_timeout
        self._clock = clock
        self._manager = None
        self._roster = None
        self._generation: int | None = None
        self._mode: str | None = None
        self._state = RUNNING
        self._stopped = False
        self._members: dict[str, dict] = {}

    @property
    def generation(self) -> int | None:
        return self._generation

    # --- протокол контура ---------------------------------------------------
    def start(self, manager, roster=None) -> None:
        self._manager, self._roster = manager, roster
        self.apply()

    def before_tick(self) -> None:
        self.apply()

    def after_tick(self) -> None:
        self.finish_tick()

    def close(self) -> None:
        self.write()

    # --- применение желаемого -------------------------------------------------
    def apply(self) -> None:
        """Граница тика или опрос: связь с ядром, поколение, состав, работа слоя брокера."""
        if self._manager is not None:
            self._manager.hold_entries(self._core_lost())
        try:
            desired = read_desired(self._core_db, self._contour_id)
        except sqlite3.Error as exc:
            log.warning("База ядра не читается (%s): контур продолжает с прежним составом", type(exc).__name__)
            desired = None
        if desired is not None:
            if desired.generation != self._generation:
                self._generation = desired.generation
                mode = None if desired.running else desired.stop_mode
                if mode != self._mode:
                    log.info("Контур %s: поколение %s — %s", self._contour_id, desired.generation,
                             "торговля" if mode is None else f"остановка {mode}")
                self._mode = mode
                self._state = RUNNING if mode is None else f"stopping_{mode}"
            if self._manager is not None:
                self._manager.stop_contour(self._mode)
            self._apply_members(desired)
        self._progress_warming()
        self._poll()

    def finish_tick(self) -> None:
        self._retire_drained()
        if self._mode is not None and not self._stopped and self._manager is not None:
            if self._manager.contour_remainder() == (0, 0) and not self._busy():
                self._stopped = True
                self._state = "stopped"
                log.info("Контур %s: остановка командой выполнена, сделок и заявок нет — завершение", self._contour_id)
                self._request_stop(f"остановка командой ({self._mode})")
        self.write()

    def _apply_members(self, desired) -> None:
        for member in desired.members:
            entry = self._members.get(member.key)
            if member.active:
                self._activate(member, entry)
            elif entry is not None and entry["state"] != REJECTED:
                self._deactivate(member, entry)
            elif entry is not None:
                self._members.pop(member.key)

    def _activate(self, member, entry) -> None:
        if entry is not None:
            entry["member"] = member
            if entry["state"] in (DRAINING, CLOSING):
                log.info("Контур %s: привязка %s возвращена в торговлю", self._contour_id, member.key)
                if self._manager is not None:
                    self._manager.stop_assignment(member.key, None)
                entry["state"] = TRADING
            return
        found = self._index.get(member.key)
        payload = member.instrument or {}
        if found is None or not payload.get("ticker"):
            reason = ("Привязки нет в конфигурации." if found is None
                      else "Инструмент привязки не определён — добавьте привязку заново.")
            log.warning("Контур %s: привязка %s не торгует — %s", self._contour_id, member.key, reason)
            self._members[member.key] = {"state": REJECTED, "member": member, "reason": reason}
            return
        kind, key, assignment = found
        target = self._share if kind == "share" else self._future
        items = target.setdefault(key, [])
        if all(item.id != assignment.id for item in items):
            items.append(assignment)
        # Прогрев и стартовый обзор — и для инструмента, которым уже торгует другая привязка:
        # обзор ставит курсор новой привязки на последний бар, история не переигрывается.
        self._members[member.key] = {"state": WARMING, "member": member, "instrument": instrument_from_payload(payload),
                                     "since": self._clock(), "warmed": False}
        log.info("Контур %s: привязка %s добавлена — прогрев", self._contour_id, member.key)

    def _deactivate(self, member, entry) -> None:
        if entry["state"] == WARMING:
            self._drop(member.key, entry)
            return
        state = CLOSING if member.remove_mode == "close_all" else DRAINING
        if entry["state"] != state:
            log.info("Контур %s: привязка %s убирается (%s)", self._contour_id, member.key, member.remove_mode)
        entry.update(state=state, member=member)
        if self._manager is not None:
            # Повтор каждый тик идемпотентен: вход мог исполниться до снятия, закрытие — не пройти.
            self._manager.stop_assignment(member.key, member.remove_mode or "no_entries")

    def _progress_warming(self) -> None:
        if self._roster is None:
            return
        now = self._clock()
        for entry in self._members.values():
            if entry["state"] != WARMING:
                continue
            instrument = entry["instrument"]
            if not entry["warmed"]:
                entry["warmed"] = self._roster.warm(instrument)
                continue
            expired = now - entry["since"] >= self._warm_timeout
            if not self._roster.ready(instrument) and not expired:
                continue
            self._roster.admit(instrument, contracts_from_payload(entry["member"].contract or {}))
            entry["state"] = TRADING
            log.info("Контур %s: привязка %s торгует", self._contour_id, entry["member"].key)

    def _retire_drained(self) -> None:
        for key, entry in list(self._members.items()):
            if entry["state"] not in (DRAINING, CLOSING) or self._manager is None:
                continue
            if self._manager.contour_remainder(assignment_id=key) != (0, 0):
                continue
            self._drop(key, entry)
            self._manager.stop_assignment(key, None)
            log.info("Контур %s: привязка %s вышла из состава — сделок нет", self._contour_id, key)

    def _drop(self, key: str, entry: dict) -> None:
        self._members.pop(key, None)
        kind, dict_key, assignment = self._index[key]
        target = self._share if kind == "share" else self._future
        target[dict_key] = [item for item in target.get(dict_key, []) if item.id != assignment.id]
        if not target[dict_key]:
            target.pop(dict_key)
        instrument = entry["instrument"]
        still = any(other.get("instrument") is not None and instrument_key(other["instrument"]) == instrument_key(instrument)
                    for other in self._members.values())
        if not still and self._roster is not None and instrument_key(instrument) in self._roster.keys():
            self._roster.retire(instrument)

    # --- состояние ------------------------------------------------------------
    def members(self) -> list[dict]:
        result = []
        for key, entry in self._members.items():
            item = {"key": key, "name": key, "state": entry["state"]}
            if entry.get("instrument") is not None:
                item["instrument"] = entry["instrument"].short_name or entry["instrument"].label
            if entry.get("reason"):
                item["reason"] = entry["reason"]
            result.append(item)
        return result

    def write(self) -> None:
        self._status(state=self._state, applied_generation=self._generation, stop_mode=self._mode,
                     members=self.members())
