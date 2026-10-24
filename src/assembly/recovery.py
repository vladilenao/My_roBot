"""Судьба контура при старте ядра: подхват, замена версии, автоподъём (add-contour-recovery).

Ядро смотрит на ``robot.pid`` папки контура, её ``status.json`` и желаемое из
``core.sqlite3``:

- живой контур (pid жив, ``status.json`` называет тот же pid, ``alive_at``
  свежий) той же версии подхватывается, другой версии — ждётся до выхода;
- живой pid без подтверждения не трогается: это может быть чужой процесс;
- без живого контура явный запуск пишет новое желаемое, а старт в ожидании
  команды поднимает контур по желаемому, если тот должен работать или не
  дожил остановку.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.assembly.contour_control import STATUS_FILE, read_status
from src.assembly.core_state import parse_time
from src.runtime_lock import LOCK_FILE, _process_alive, _read_pid

ADOPT = "adopt"          # живой контур той же версии — подхват
REPLACE = "replace"      # живой контур другой версии или уже выходящий — ждать выхода, затем решать заново
BLOCKED = "blocked"      # живой pid без подтверждения — не трогать, контур не запускать
LAUNCH = "launch"        # явный запуск — новое желаемое
AUTOSTART = "autostart"  # автоподъём по желаемому
WAIT = "wait"            # ждать команду

RECOVERY_AUTOSTART, RECOVERY_ADOPTED = "autostart", "adopted"


@dataclass(frozen=True)
class RecoveryPlan:
    action: str
    pid: int | None = None
    status: dict | None = None
    members: tuple = ()

    def instruments(self):
        from src.assembly.contracts import instrument_from_payload

        return [instrument_from_payload(member.instrument) for member in self.members]


def _confirmed(status: dict | None, pid: int, timeout: float, now: datetime) -> bool:
    if not status or status.get("pid") != pid or status.get("exited_at") or not status.get("alive_at"):
        return False
    try:
        return (now - parse_time(status["alive_at"])).total_seconds() <= timeout
    except (TypeError, ValueError):
        return False


def should_run(desired, status: dict | None) -> bool:
    """Желаемое «работает» или остановка, не дожившая до ``stopped``."""
    if desired is None:
        return False
    if desired.running:
        return True
    return bool(status) and str(status.get("state") or "").startswith("stopping_")


def autostart_members(desired, status: dict | None) -> tuple:
    """Состав автоподъёма: активные и снятые, которых контур ещё держал (у них были сделки)."""
    held = None if not status else {item.get("key") for item in status.get("members") or ()}
    return tuple(member for member in desired.members
                 if member.active or held is None or member.key in held)


def plan_recovery(directory: Path, *, version: str, desired, explicit: bool, autostart: bool, timeout: float,
                  alive=_process_alive, now: datetime | None = None) -> RecoveryPlan:
    directory = Path(directory)
    status = read_status(directory / STATUS_FILE)
    pid = _read_pid(directory / LOCK_FILE)
    if pid is not None and alive(pid):
        if status and status.get("pid") == pid and status.get("exited_at"):
            return RecoveryPlan(REPLACE, pid, status)   # контур отметил выход и закрывается
        if not _confirmed(status, pid, timeout, now or datetime.now(timezone.utc)):
            return RecoveryPlan(BLOCKED, pid, status)
        if status.get("version") == version:
            members = () if desired is None else autostart_members(desired, status)
            return RecoveryPlan(ADOPT, pid, status, members)
        return RecoveryPlan(REPLACE, pid, status)
    if explicit:
        return RecoveryPlan(LAUNCH, status=status)
    if autostart and should_run(desired, status) and desired.members:
        return RecoveryPlan(AUTOSTART, status=status, members=autostart_members(desired, status))
    return RecoveryPlan(WAIT, status=status)
