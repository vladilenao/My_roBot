"""Пределы процесса при запуске робота (fix-sqlite-connection-close).

«Терминал» macOS даёт процессу мягкий предел 256 открытых файлов. Ядру с
прогревом двух десятков инструментов этого мало: базы, сокеты брокера и Redis
вместе превышают его в первую минуту. Поэтому каждый процесс робота поднимает
мягкий предел сам, не дожидаясь ``ulimit -n`` от пользователя.
"""

from __future__ import annotations

from dataclasses import dataclass

try:  # на Windows модуля нет, и предела открытых файлов в этом виде тоже
    import resource
except ImportError:  # pragma: no cover - проверяется подменой в тестах
    resource = None

OPEN_FILES_TARGET = 8192


@dataclass(frozen=True)
class OpenFilesLimit:
    """Итог шага: мягкий предел до и после, ошибка, если поднять не удалось."""

    before: int | None
    after: int | None
    error: str | None = None

    @property
    def raised(self) -> bool:
        return self.before is not None and self.after is not None and self.after > self.before


_DEFAULT = object()


def raise_open_files_limit(target: int = OPEN_FILES_TARGET, module=_DEFAULT) -> OpenFilesLimit:
    """Поднимает мягкий ``RLIMIT_NOFILE`` до ``min(target, жёсткий)``, если он ниже.

    Ошибку не бросает: процесс продолжает запуск с прежним пределом.
    """
    module = resource if module is _DEFAULT else module
    if module is None:
        return OpenFilesLimit(None, None)
    try:
        soft, hard = module.getrlimit(module.RLIMIT_NOFILE)
    except (OSError, ValueError) as exc:
        return OpenFilesLimit(None, None, type(exc).__name__)
    wanted = target if hard == module.RLIM_INFINITY else min(target, hard)
    if soft == module.RLIM_INFINITY or soft >= wanted:
        return OpenFilesLimit(soft, soft)
    try:
        module.setrlimit(module.RLIMIT_NOFILE, (wanted, hard))
    except (OSError, ValueError) as exc:
        return OpenFilesLimit(soft, soft, type(exc).__name__)
    return OpenFilesLimit(soft, wanted)


def describe(limit: OpenFilesLimit) -> tuple[str, str] | None:
    """Уровень и текст строки журнала об итоге шага; ``None`` — писать нечего."""
    if limit.error is not None:
        return "warning", (f"Предел открытых файлов не поднят ({limit.error}): осталось {limit.before}; "
                           f"при сбоях «Too many open files» запускайте после ulimit -n {OPEN_FILES_TARGET}")
    if limit.raised:
        return "info", f"Предел открытых файлов поднят: {limit.before} → {limit.after}"
    return None
