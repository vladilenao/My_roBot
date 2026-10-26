"""Прогоны истории — контуры вида ``history`` (add-history-contour).

Сторона робота — ``HistoryRuns``: очередь не больше ``max_history_runs``
прогонов сразу, сверка импорта эмулятора данных по одной перед каждым
стартом, процесс ``--contour history <имя>`` с выводом в ``console.log``,
журнал прогонов в ``core.sqlite3`` (``history_runs``). Очередь работает своим
потоком и не зависит от готовности ядра: свечи прогону даёт эмулятор данных.

Сторона процесса прогона — ``HistoryProgress``: ``status.json`` (фаза, ход по
рыночному времени, отказы, признак жизни, итог) и остановка после тика по
stdin, Ctrl+C, пульсу робота ``stopped`` или флагу своей строки.
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.assembly.contour_control import STATUS_FILE, read_status, write_status
from src.assembly.core_state import (
    RUN_CANCELLED, RUN_FAILED, RUN_FINISHED, RUN_INTERRUPTED, RUN_QUEUED, RUN_RUNNING, RUN_STARTING, RUN_STOPPED,
    RUN_STOPPING, parse_time, read_heartbeat, read_history_stop,
)
from src.assembly.process import LAUNCH_FILE, contour_dir, write_launch
from src.logging_setup import get_logger
from src.runtime_lock import LOCK_FILE, _process_alive, _read_pid

log = get_logger(__name__)

HISTORY_KIND = "history"
RUN_PREFIX = "history-"
CONSOLE_LOG = "console.log"
REPORT_JSON = "report.json"
LIST_LIMIT_DEFAULT = 20
LIST_LIMIT_MAX = 200
OPERATOR_STOP = "остановлено оператором"
# Фазы процесса прогона в status.json.
PHASE_CHECKING, PHASE_RUNNING, PHASE_FINISHING = "checking", "running", "finishing"
# Что остаётся в папке прогона при повторе после сдвига начала.
_KEEP = (LAUNCH_FILE, STATUS_FILE, STATUS_FILE + ".tmp", CONSOLE_LOG, LOCK_FILE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def history_dir(data_dir: Path) -> Path:
    return contour_dir(data_dir, HISTORY_KIND)


def run_dir(data_dir: Path, run_id: str) -> Path:
    return history_dir(data_dir) / run_id.removeprefix(RUN_PREFIX)


def clear_attempt(directory: Path, log_file: str) -> None:
    """Повтор после сдвига начала: убрать журнал и выбор прошлой попытки, оставить запуск, статус и логи."""
    for item in Path(directory).iterdir():
        if item.name in _KEEP or item.name.startswith(log_file):
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


def prepare_import(provider):
    """Сверка импорта My Robot в эмуляторе данных: ``(MarketDataSync, номер снимка, строка для консоли)``."""
    from src.api.emulator_client import EmulatorError
    from src.history.report import MarketDataSync

    try:
        sync = provider.prepare_snapshot()
    except EmulatorError as exc:
        return MarketDataSync(reason=exc.message), None, f"импорт My Robot не подтверждён: {exc.message}"
    if sync["synchronized"]:
        result = MarketDataSync(status="synchronized", producer_id=sync["producer_id"],
                                target_change_id=sync["target_change_id"], after_id=sync["after_id"],
                                snapshot_generation=sync["snapshot_generation"])
        return result, sync["snapshot_generation"], (
            f"импорт My Robot подтверждён: снимок {sync['snapshot_generation']}, "
            f"курсор {sync['after_id']}/{sync['target_change_id']}")
    source = sync.get("source")
    state = source.get("state") if isinstance(source, dict) else None
    reason = (f"эмулятор сообщил состояние {state}" if state in {"incomplete", "blocked", "error"}
              else "эмулятор не подтвердил полноту журнала")
    result = MarketDataSync(producer_id=sync["producer_id"], target_change_id=sync["target_change_id"],
                            after_id=sync["after_id"], reason=reason)
    return result, None, f"импорт My Robot не подтверждён: {reason}"


def _money(amount) -> dict | None:
    """Сумма итога ``report.json`` (строка) → ``Money`` контракта; суммы прогона — в рублях."""
    return None if amount is None else {"amount": str(amount), "currency": "RUB"}


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _confirmed(status: dict | None, pid: int, timeout: float, now: datetime) -> bool:
    if not status or status.get("pid") != pid or status.get("exited_at") or not status.get("alive_at"):
        return False
    try:
        return (now - parse_time(status["alive_at"])).total_seconds() <= timeout
    except (TypeError, ValueError):
        return False


def _progress(status: dict) -> float | None:
    try:
        start, end = parse_time(status["start"]), parse_time(status["end"])
        now = parse_time(status["market_now"])
    except (KeyError, TypeError, ValueError):
        return None
    total = (end - start).total_seconds()
    if total <= 0:
        return None
    return round(min(max((now - start).total_seconds() / total, 0.0), 1.0), 4)


class HistoryRuns:
    """Очередь и надзор прогонов истории в процессе робота."""

    def __init__(self, store, data_dir: Path, *, max_runs: int, version: str, heartbeat_timeout: float,
                 share_strategies: dict, future_strategies: dict, log_file: str = "bot_debug.log",
                 provider_factory=None, prepare=prepare_import, process_factory=None, adopt_factory=None,
                 alive=_process_alive, say=print) -> None:
        self._store = store
        self._data_dir = Path(data_dir)
        self.max_runs = max_runs
        self._version = version
        self._heartbeat_timeout = heartbeat_timeout
        self._share = share_strategies
        self._future = future_strategies
        self._log_file = log_file
        self._provider_factory = provider_factory
        self._prepare = prepare
        self._process_factory = process_factory
        self._adopt_factory = adopt_factory
        self._alive = alive
        self._say = say
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._processes: dict = {}
        self._thread: threading.Thread | None = None

    # --- запуск и остановка робота -----------------------------------------
    def recover(self) -> None:
        """Старт робота: очередь не возобновляется, живой прогон подхватывается по pid, прочие — ``interrupted``."""
        now = datetime.now(timezone.utc)
        for run in self._store.history_runs(active=True):
            if run.state in (RUN_QUEUED, RUN_STARTING):
                self._store.update_history_run(run.run_id, state=RUN_CANCELLED, finished_at=_now(),
                                               reason="робот перезапущен")
                continue
            directory = run_dir(self._data_dir, run.run_id)
            pid = _read_pid(directory / LOCK_FILE)
            status = read_status(directory / STATUS_FILE)
            if pid is not None and self._alive(pid) and _confirmed(status, pid, self._heartbeat_timeout, now):
                self._adopt(run.run_id, pid)
                continue
            self._store.update_history_run(run.run_id, state=RUN_INTERRUPTED, finished_at=_now(), pid=None,
                                           reason="робот перезапущен, процесса прогона нет")

    def _adopt(self, run_id: str, pid: int) -> None:
        from src.assembly.process import AdoptedProcess

        factory = self._adopt_factory or AdoptedProcess
        process = factory(HISTORY_KIND, pid, on_exit=lambda code, stopping: self._exited(run_id, None, stopping))
        with self._lock:
            self._processes[run_id] = process
        process.start()
        log.info("Прогон %s подхвачен живым, pid %s", run_id, pid)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="history-queue", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Остановка робота: очередь отменяется, идущие прогоны пишут отчёт, не вышедшие — принудительно."""
        self._halt.set()
        self._wake.set()
        for run in self._store.history_runs(active=True):
            if run.state == RUN_QUEUED:
                self._store.update_history_run(run.run_id, state=RUN_CANCELLED, finished_at=_now(),
                                               reason="робот остановлен")
        with self._lock:
            processes = dict(self._processes)
        for run_id, process in processes.items():
            self._store.update_history_run(run_id, state=RUN_STOPPING, stop_requested=True)
            process.request_stop()
        for process in processes.values():
            process.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # --- API ------------------------------------------------------------------
    # ``id`` прогона в API — имя его папки, без префикса ``history-`` (rework-contour-api);
    # ключ строки ``history_runs`` прежний, поэтому строки прежней версии читаются без миграции.
    def submit(self, request) -> tuple[int, dict]:
        """Прогон из команды API: привязки и конфликт проверяются сразу, остальное — процессом прогона."""
        from src.bot.launch import classify_tokens

        _, rejections = classify_tokens(request.instruments, self._share, self._future)
        if rejections:
            return 422, {"code": "INSTRUMENTS_REJECTED", "message": "Прогон не поставлен в очередь.",
                         "rejections": [{"token": item.token, "reason": item.reason} for item in rejections]}
        payload = {"from": f"{request.start:%Y-%m-%d %H:%M}", "to": f"{request.end:%Y-%m-%d %H:%M}",
                   "pause": float(request.pause), "instruments": list(request.instruments)}
        with self._lock:
            name = self._free_name(datetime.now())
            run = self._store.add_history_run(RUN_PREFIX + name, f"{name}", payload)
        log.info("Прогон %s принят: %s — %s, %s", name, payload["from"], payload["to"],
                 ",".join(payload["instruments"]))
        self._wake.set()
        return 202, self.payload(run)

    def _free_name(self, moment: datetime) -> str:
        base = moment.strftime("%Y%m%d-%H%M%S")
        name, number = base, 1
        while self._store.history_run(RUN_PREFIX + name) is not None or (history_dir(self._data_dir) / name).exists():
            number += 1
            name = f"{base}-{number}"
        return name

    def runs(self, state: str | None = None, limit: int = LIST_LIMIT_DEFAULT) -> list[dict]:
        """Прогоны в очереди и в работе (по очереди) и ``limit`` последних завершённых; ``state`` — active/finished."""
        runs = [] if state == "finished" else self._store.history_runs(active=True)
        if state != "active":
            runs += self._store.history_runs(active=False, limit=limit)
        return [self.payload(run) for run in runs]

    def run(self, run_id: str) -> dict | None:
        run = self._store.history_run(RUN_PREFIX + run_id)
        return None if run is None else self.payload(run)

    def limits(self) -> dict:
        active = self._store.history_runs(active=True)
        return {"max": self.max_runs,
                "running": sum(run.state != RUN_QUEUED for run in active),
                "queued": sum(run.state == RUN_QUEUED for run in active)}

    def stop(self, run_id: str) -> tuple[int, dict]:
        """Из очереди — ``cancelled``; идущий — ``stopping``, отчёт пишет процесс; повтор — ``200``."""
        key = RUN_PREFIX + run_id
        run = self._store.history_run(key)
        if run is None:
            return 404, {"code": "NOT_FOUND", "message": f"Прогона {run_id!r} нет."}
        if not run.active:
            return 409, {"code": "NOT_RUNNING", "message": "Прогон завершён: останавливать нечего."}
        if run.state == RUN_QUEUED:
            run = self._store.update_history_run(key, state=RUN_CANCELLED, finished_at=_now(),
                                                 reason="убран из очереди командой")
            log.info("Прогон %s убран из очереди", run_id)
            return 202, self.payload(run)
        if run.stop_requested or run.state == RUN_STOPPING:
            return 200, self.payload(run)
        run = self._store.update_history_run(key, state=RUN_STOPPING, stop_requested=True)
        with self._lock:
            process = self._processes.get(key)
        if process is not None:
            process.request_stop()
        log.info("Прогон %s останавливается командой", run_id)
        return 202, self.payload(run)

    def report(self, run_id: str) -> tuple[int, dict]:
        """Итог из ``report.json``: только у завершённого прогона, иначе ``404 REPORT_NOT_READY``."""
        from src.bot import control_views as views

        run = self._store.history_run(RUN_PREFIX + run_id)
        if run is None:
            return 404, {"code": "NOT_FOUND", "message": f"Прогона {run_id!r} нет."}
        report = None if run.active else _read_json(run_dir(self._data_dir, run.run_id) / REPORT_JSON)
        if report is None:
            return 404, {"code": "REPORT_NOT_READY", "message": "Прогон ещё не завершён или отчёт не записан."}
        summary = report.get("result") or {}
        return 200, {
            "covered": bool(report.get("covered")), "stop_reason": report.get("stop_reason") or "",
            "trades": summary.get("trades"), "net_pnl": _money(summary.get("net_pnl")),
            "fees": _money(summary.get("fees")), "balance": _money(summary.get("balance")),
            "open_positions": list(summary.get("open_positions") or ()),
            "_links": views.links(run=views.run_href(run_id)),
        }

    def log_tail(self, run_id: str, count: int) -> str | None:
        from src.bot.launch_control import tail_lines

        if self._store.history_run(RUN_PREFIX + run_id) is None:
            return None
        return tail_lines(run_dir(self._data_dir, run_id) / self._log_file, count)

    def payload(self, run) -> dict:
        """Прогон в форме ``HistoryRun`` контракта: ход по ``status.json``, процесс, ссылки и действия."""
        from src.bot import control_views as views

        run_id = run.run_id.removeprefix(RUN_PREFIX)
        directory = run_dir(self._data_dir, run.run_id)
        status = read_status(directory / STATUS_FILE) if run.state != RUN_QUEUED else None
        position = None
        if run.state == RUN_QUEUED:
            queued = [item.run_id for item in self._store.history_runs(active=True) if item.state == RUN_QUEUED]
            position = queued.index(run.run_id) + 1 if run.run_id in queued else None
        sync = run.market_data_sync
        progress = None if status is None else {
            "phase": status.get("phase"), "start": status.get("start"), "end": status.get("end"),
            "market_now": status.get("market_now"), "ticks": status.get("ticks"), "fraction": _progress(status)}
        process = None if run.started_at is None or run.state == RUN_CANCELLED and run.pid is None else {
            "pid": run.pid if run.active else None, "exit_code": run.exit_code,
            "exited_at": (status or {}).get("exited_at") or (None if run.active else run.finished_at),
            "dir": directory.relative_to(self._data_dir).as_posix()}
        href = views.run_href(run_id)
        links = views.links(self=href, log=href + "/log", collection=views.HISTORY)
        if not run.active and (directory / REPORT_JSON).is_file():
            links["report"] = {"href": href + "/report"}
        return {
            "id": run_id, "state": run.state, "request": run.request,
            "accepted_at": run.accepted_at, "started_at": run.started_at, "finished_at": run.finished_at,
            "queue_position": position, "reason": run.reason,
            "market_data_sync": None if sync is None else {"status": sync.get("status"),
                                                           "snapshot_generation": sync.get("snapshot_generation")},
            "progress": progress, "process": process,
            "_links": links, "_actions": views.run_action_list(run_id, views.run_actions(run.state)),
        }

    # --- поток очереди ---------------------------------------------------------
    def _run(self) -> None:
        while not self._halt.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if self._halt.is_set():
                return
            try:
                self._fill()
            except Exception:  # поток очереди не должен умирать: прогоны остались бы в queued навсегда
                log.exception("Очередь прогонов истории: сбой запуска")

    def _fill(self) -> None:
        while not self._halt.is_set():
            with self._lock:
                running = len(self._processes)
            queued = [run for run in self._store.history_runs(active=True) if run.state == RUN_QUEUED]
            if running >= self.max_runs or not queued:
                return
            self._launch(queued[0])

    def _launch(self, run) -> None:
        from src import __version__
        from src.assembly.process import ContourProcess, contour_command

        run_id = run.run_id
        directory = run_dir(self._data_dir, run_id)
        self._store.update_history_run(run_id, state=RUN_STARTING, started_at=_now())
        provider = self._provider_factory() if self._provider_factory is not None else _emulator_provider()
        sync, generation, message = self._prepare(provider)
        self._say(f"Прогон {run_id}: {message}")
        current = self._store.history_run(run_id)
        if current is None or current.stop_requested or self._halt.is_set():
            self._store.update_history_run(run_id, state=RUN_CANCELLED, finished_at=_now(),
                                           reason="остановлен до старта процесса")
            return
        from dataclasses import asdict

        sync_payload = asdict(sync)
        try:
            write_launch(directory, {
                "contour_id": run_id, "kind": HISTORY_KIND, "core_pid": _pid(), "core_version": __version__,
                "from": run.request["from"], "to": run.request["to"], "pause": run.request["pause"],
                "instruments": run.request["instruments"], "market_data_sync": sync_payload,
                "snapshot_generation": generation,
            })
            factory = self._process_factory or ContourProcess
            process = factory(HISTORY_KIND, command=contour_command(HISTORY_KIND, directory.name),
                              on_exit=lambda code, stopping: self._exited(run_id, code, stopping),
                              output=directory / CONSOLE_LOG)
            with self._lock:
                self._processes[run_id] = process
            pid = process.start()
        except Exception as exc:
            with self._lock:
                self._processes.pop(run_id, None)
            log.exception("Прогон %s не запущен", run_id)
            self._store.update_history_run(run_id, state=RUN_FAILED, finished_at=_now(), market_data_sync=sync_payload,
                                           reason=f"процесс прогона не запустился: {type(exc).__name__}")
            self._say(f"Прогон {run_id} не запущен: {type(exc).__name__}")
            return
        self._store.update_history_run(run_id, state=RUN_RUNNING, pid=pid, market_data_sync=sync_payload)
        request = run.request
        self._say(f"Прогон {run_id} запущен: {request['from']} — {request['to']}, "
                  f"{','.join(request['instruments'])}, pid {pid}")

    def _exited(self, run_id: str, code: int | None, stopping: bool) -> None:
        """Итог прогона по коду выхода и ``status.json``; освободившееся место — следующему в очереди."""
        with self._lock:
            self._processes.pop(run_id, None)
        run = self._store.history_run(run_id)
        status = read_status(run_dir(self._data_dir, run_id) / STATUS_FILE) or {}
        if code is None:
            # Подхваченный процесс: кода выхода нет, итог процесс записал в status.json.
            code = status.get("exit_code") if status.get("exited_at") else None
        stopped = stopping or (run is not None and run.stop_requested)
        if code == 0:
            state, reason = (RUN_STOPPED, OPERATOR_STOP) if stopped else (RUN_FINISHED, None)
        else:
            state = RUN_FAILED
            reason = status.get("reason") or ("процесс прогона завершился без отметки выхода" if code is None
                                              else f"процесс прогона завершился с кодом {code}")
        self._store.update_history_run(run_id, state=state, finished_at=_now(), exit_code=code, reason=reason)
        self._say(f"Прогон {run_id}: {state}" + (f" — {reason}" if reason and state == RUN_FAILED else ""))
        self._wake.set()


def _pid() -> int:
    import os
    return os.getpid()


def _emulator_provider():
    from src.api.emulator_client import EmulatorClientProvider
    return EmulatorClientProvider()


class HistoryProgress:
    """Процесс прогона: ``status.json`` и остановка после тика (add-history-contour)."""

    def __init__(self, directory: Path, *, run_id: str, core_db: Path, version: str, interval: float,
                 pid: int | None = None, stop_event: threading.Event | None = None,
                 read_stop=read_history_stop, read_beat=read_heartbeat) -> None:
        import os

        self._path = Path(directory) / STATUS_FILE
        self._run_id = run_id
        self._core_db = Path(core_db)
        self._version = version
        self._interval = interval
        self._pid = pid if pid is not None else os.getpid()
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._read_stop = read_stop
        self._read_beat = read_beat
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase = PHASE_CHECKING
        self._clock = None
        self._session = None
        self._rejections: list[dict] = []
        self._reason: str | None = None
        self.stop_reason = ""
        self._exit: dict = {}

    # --- путь истории ------------------------------------------------------------
    def attach(self, clock, session) -> None:
        """Новая попытка прогона: свои часы и сессия; уже запрошенный стоп действует сразу."""
        with self._lock:
            self._clock, self._session = clock, session
            self._phase = PHASE_CHECKING
            reason = self.stop_reason
        if reason:
            session.request_stop(reason)
        self.write()

    def phase(self, name: str) -> None:
        with self._lock:
            self._phase = name
        self.write()

    def reject(self, rejections) -> None:
        with self._lock:
            self._rejections = [{"token": item.token, "reason": item.reason} for item in rejections]
            self._reason = "; ".join(f"{item.token}: {item.reason}" for item in rejections) or "инструменты не выбраны"
        self.write()

    def fail(self, reason: str) -> None:
        with self._lock:
            self._reason = reason
        self.write()

    def request_stop(self, reason: str = OPERATOR_STOP) -> None:
        with self._lock:
            if self.stop_reason:
                return
            self.stop_reason = reason
            session = self._session
        log.info("Прогон %s останавливается после тика: %s", self._run_id, reason)
        if session is not None:
            session.request_stop(reason)

    # --- поток признака жизни и стопа ------------------------------------------
    def start(self) -> None:
        # Первая проверка — сразу: стоп мог прийти, пока процесс запускался.
        self.check()
        self._thread = threading.Thread(target=self._loop, name="history-progress", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import time

        last = 0.0
        while not self._halt.wait(0.5):
            if self._stop_event.is_set():
                self.request_stop()
            if time.monotonic() - last < self._interval:
                continue
            last = time.monotonic()
            try:
                self.check()
            except Exception:  # поток не должен умирать молча: прогон остался бы без признака жизни
                log.exception("Проверка стопа прогона не выполнена")

    def check(self) -> None:
        """Пульс робота ``stopped`` и флаг стопа своей строки; затем ``alive_at``."""
        beat = self._read_beat(self._core_db)
        if beat is not None and beat.stopped:
            self.request_stop()
        elif self._read_stop(self._core_db, self._run_id):
            self.request_stop()
        self.write()

    def finish(self, code: int) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            self._exit = {"exited_at": _now(), "exit_code": code,
                          "exit_reason": self.stop_reason or ("прогон завершён" if code == 0 else self._reason)}
        self.write()

    def write(self) -> None:
        with self._lock:
            clock, session = self._clock, self._session
            payload = {
                "contour_id": self._run_id, "kind": HISTORY_KIND, "pid": self._pid, "version": self._version,
                "alive_at": _now(), "phase": self._phase, "rejections": list(self._rejections),
                "reason": self._reason, **self._exit,
            }
        if clock is not None:
            payload["start"] = _iso(clock.start)
            payload["end"] = _iso(clock.end)
            payload["market_now"] = _iso(clock.now())
            payload["ticks"] = session.ticks_done() if session is not None else 0
        try:
            write_status(self._path, payload)
        except OSError as exc:
            log.warning("status.json прогона не записан: %s", type(exc).__name__)


def _iso(moment) -> str:
    value = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
