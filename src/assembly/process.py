"""Процесс торгового контура: файл запуска, переезд журнала и надзор (add-contour-process).

Ядро после выбора инструментов пишет ``launch.json`` в папку контура и
запускает процесс тем же исполняемым файлом с ``--contour <вид>``. Канал
stdin держится открытым: строка ``stop`` — штатная остановка после тика,
конец потока — ядро завершилось (D1, D2, D5). Журнал эмуляции один раз
переезжает из ``data/`` в папку контура, исходные файлы остаются для отката
(D6). Модуль не импортирует контур.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.logging_setup import get_logger

log = get_logger(__name__)

LAUNCH_FILE = "launch.json"
LAUNCH_SCHEMA_VERSION = 1
MIGRATION_FILE = "migrated-from.json"
STOP_TIMEOUT_SECS = 30.0
CONTOURS_DIR = "contours"
# Журнал эмуляции, который переезжает в папку контура: SQLite — через backup.
JOURNAL_DATABASES = ("trades.sqlite3", "telegram_delivery.sqlite3")
JOURNAL_FILES = ("trade_event.csv", "trade_summary.csv", "trade_decision_trace.log")
JOURNAL_DIRS = ("telegram-diagnostics",)


def contour_dir(data_dir: Path, kind: str) -> Path:
    return Path(data_dir) / CONTOURS_DIR / kind


class LaunchFileError(RuntimeError):
    """Файл запуска отсутствует, повреждён или от другой версии формата."""


def write_launch(directory: Path, payload: dict) -> Path:
    """Атомарная запись ``launch.json``: временный файл и переименование."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / LAUNCH_FILE
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"schema_version": LAUNCH_SCHEMA_VERSION, **payload}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    os.replace(temporary, target)
    return target


def read_launch(directory: Path, contour_id: str) -> dict:
    """Файл запуска контура; другая версия формата или чужой контур — ``LaunchFileError``."""
    path = Path(directory) / LAUNCH_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LaunchFileError(f"Нет файла запуска {path}: контур запускает ядро робота") from None
    except (OSError, ValueError) as exc:
        raise LaunchFileError(f"Файл запуска {path} не читается: {type(exc).__name__}") from None
    if payload.get("schema_version") != LAUNCH_SCHEMA_VERSION:
        raise LaunchFileError(
            f"Файл запуска версии {payload.get('schema_version')!r}, контур понимает {LAUNCH_SCHEMA_VERSION}: "
            "ядро и контур разных версий")
    if payload.get("contour_id") != contour_id:
        raise LaunchFileError(f"Файл запуска для контура {payload.get('contour_id')!r}, а запущен {contour_id!r}")
    return payload


class JournalMigrationError(RuntimeError):
    """Журнал не скопирован в папку контура; контур не запускается."""


def migrate_journal(data_dir: Path, target: Path, *, version: str) -> bool:
    """Разовый переезд журнала эмуляции в папку контура (D6).

    ``True`` — журнал скопирован сейчас; ``False`` — переезд не нужен (журнал уже
    в папке контура или в ``data/`` его нет). Исходные файлы не удаляются.
    """
    data_dir, target = Path(data_dir), Path(target)
    if (target / "trades.sqlite3").exists() or not (data_dir / "trades.sqlite3").exists():
        return False
    created = not target.exists()
    staging = target.with_name(target.name + ".migrating")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    copied: list[str] = []
    try:
        for name in JOURNAL_DATABASES:
            source = data_dir / name
            if source.exists():
                with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30) as src, \
                        sqlite3.connect(staging / name) as dst:
                    src.backup(dst)
                copied.append(name)
        for path in sorted(data_dir.glob("trade_decision_trace.log*")) + [data_dir / n for n in JOURNAL_FILES[:2]]:
            if path.is_file() and not (staging / path.name).exists():
                shutil.copy2(path, staging / path.name)
                copied.append(path.name)
        for name in JOURNAL_DIRS:
            if (data_dir / name).is_dir():
                shutil.copytree(data_dir / name, staging / name)
                copied.append(name + "/")
        (staging / MIGRATION_FILE).write_text(json.dumps({
            "from": str(data_dir), "files": copied, "version": version,
            "migrated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        target.mkdir(parents=True, exist_ok=True)
        for item in staging.iterdir():
            os.replace(item, target / item.name)
        staging.rmdir()
    except BaseException as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise JournalMigrationError(
            f"Журнал эмуляции не перенесён в {target}: {type(exc).__name__}. Файлы в {data_dir} не изменены") from exc
    log.info("Журнал эмуляции перенесён в %s (%d файлов), копия для отката осталась в %s",
             target, len(copied), data_dir)
    return True


def contour_command(kind: str) -> list[str]:
    """Команда процесса контура: тот же исполняемый файл с ``--contour <вид>``."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--contour", kind]
    entry = Path(__file__).resolve().parents[2] / "run.py"
    return [sys.executable, str(entry), "--contour", kind]


class ContourProcess:
    """Надзор ядра за процессом контура: запуск, stdin, остановка, итог (D5)."""

    def __init__(self, kind: str, *, command: list[str] | None = None, on_exit=None,
                 popen=subprocess.Popen, stop_timeout: float = STOP_TIMEOUT_SECS) -> None:
        self.kind = kind
        self._command = command or contour_command(kind)
        self._on_exit = on_exit
        self._popen = popen
        self._stop_timeout = stop_timeout
        self._process = None
        self._stopping = False
        self._watcher: threading.Thread | None = None
        self.exit_code: int | None = None

    @property
    def pid(self) -> int | None:
        return None if self._process is None or self.exit_code is not None else self._process.pid

    @property
    def running(self) -> bool:
        return self._process is not None and self.exit_code is None

    def start(self) -> int:
        # Вывод контура — в консоль ядра (наследуется); stdin — канал жизни.
        self._process = self._popen(self._command, stdin=subprocess.PIPE, text=True, encoding="utf-8")
        self._watcher = threading.Thread(target=self._watch, name=f"contour-{self.kind}-watch", daemon=True)
        self._watcher.start()
        log.info("Процесс контура %s запущен, pid %s", self.kind, self._process.pid)
        return self._process.pid

    def _watch(self) -> None:
        code = self._process.wait()
        self.exit_code = code
        if code == 0:
            log.info("Процесс контура %s завершился", self.kind)
        else:
            message = f"Процесс контура {self.kind} завершился с кодом {code}; торговля не перезапускается"
            log.error(message)
            print(message)
        if self._on_exit is not None:
            self._on_exit(code, self._stopping)

    def stop(self) -> int | None:
        """``stop`` в stdin, ожидание до ``stop_timeout``, затем принудительное завершение."""
        if self._process is None:
            return None
        self._stopping = True
        if self.exit_code is None:
            try:
                self._process.stdin.write("stop\n")
                self._process.stdin.flush()
                self._process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self._process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                log.error("Контур %s не остановился за %.0f с — завершается принудительно",
                          self.kind, self._stop_timeout)
                self._process.kill()
                self._process.wait()
        if self._watcher is not None:
            self._watcher.join(timeout=5)
        return self.exit_code


class StdinStop:
    """Сторона контура: ``stop`` или конец stdin ставят флаг остановки на границе тика."""

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdin
        self.requested = threading.Event()
        self.reason = ""

    def start(self) -> None:
        threading.Thread(target=self._read, name="contour-stdin", daemon=True).start()

    def request(self, reason: str) -> None:
        if not self.requested.is_set():
            self.reason = reason
            log.info("Контур останавливается после текущего тика: %s", reason)
            self.requested.set()

    def _read(self) -> None:
        try:
            for line in self._stream:
                if line.strip() == "stop":
                    self.request("команда ядра")
                    return
        except (OSError, ValueError):
            pass
        self.request("ядро завершилось")
