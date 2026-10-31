"""Представление API управления (rework-contour-api): ссылки, допустимые действия, ошибки.

Слова API — слова оператора: ``target`` / ``current`` / ``revision``. Внутренние
``desired`` / ``actual`` / ``generation`` из ``core.sqlite3`` и ``status.json``
переводятся в них в ``LaunchControl`` и ``HistoryRuns``.

Набор действий выводится функциями ``emulation_actions``, ``member_actions`` и
``run_actions``. Те же функции решают, примет ли сервер команду, поэтому
кнопка в ``_actions`` не расходится с ответом на её нажатие.
"""

from __future__ import annotations

from urllib.parse import quote

HOME = "/api/v1"
CORE = "/api/v1/core"
EMULATION = "/api/v1/emulation"
EMULATION_TARGET = EMULATION + "/target"
EMULATION_INSTRUMENTS = EMULATION + "/instruments"
EMULATION_REMOVE = EMULATION_INSTRUMENTS + "/remove"
EMULATION_LOG = EMULATION + "/log"
HISTORY = "/api/v1/history"
# Контур «Песочница» (add-sandbox-contour): состав — привязки, а не инструменты.
SANDBOX = "/api/v1/sandbox"
SANDBOX_TARGET = SANDBOX + "/target"
SANDBOX_BINDINGS = SANDBOX + "/bindings"
SANDBOX_REMOVE = SANDBOX_BINDINGS + "/remove"
SANDBOX_RECONCILE = SANDBOX + "/reconcile"
SANDBOX_LOG = SANDBOX + "/log"
# Состояние привязки в процессе контура на брокере (add-tinvest-orders); watching — режим только чтения.
BINDING_STATES = ("warming", "trading", "draining", "closing", "rejected", "watching")
# Свечи рыночной базы ядра для графиков UI (add-candles-api): только чтение.
CANDLES = "/api/v1/candles"

MODE_ON = "on"
NO_ENTRIES = "no_entries"
CLOSE_ALL = "close_all"

PROBLEM_TYPE = "urn:my-robot:problem:"
PROBLEM_TITLES = {
    "INVALID_REQUEST": "Неверный запрос",
    "UNAUTHORIZED": "Нет доступа",
    "FORBIDDEN_HOST": "Чужой адрес сервера",
    "NOT_FOUND": "Не найдено",
    "REPORT_NOT_READY": "Отчёта ещё нет",
    "ALREADY_RUNNING": "Уже работает",
    "STOPPING": "Останавливается",
    "NOT_RUNNING": "Не работает",
    "MODE_CONFLICT": "Режим не вернуть",
    "CORE_NOT_READY": "Ядро не готово",
    "CONTOUR_DIR_BUSY": "Папка контура занята",
    "REVISION_CHANGED": "Целевое состояние уже изменилось",
    "INSTRUMENTS_REJECTED": "Инструменты отклонены",
    "BINDINGS_REJECTED": "Привязки отклонены",
    "HISTORY_UNAVAILABLE": "Очередь истории недоступна",
    "INTERNAL": "Внутренняя ошибка",
    "MARKET_DATA_NOT_READY": "Рыночная база не готова",
}
_CORE_BRIEF = ("state", "error", "attempts", "next_retry_at")


def links(**items: str) -> dict:
    """``rel=href`` → ``{"rel": {"href": …}}``; подчёркивание в имени связи — дефис."""
    return {name.replace("_", "-"): {"href": href} for name, href in items.items()}


def member_href(name: str) -> str:
    return f"{EMULATION_INSTRUMENTS}/{quote(name, safe='')}"


def run_href(run_id: str) -> str:
    return f"{HISTORY}/{run_id}"


def etag(revision: int | None) -> dict | None:
    return None if revision is None else {"ETag": f'"{revision}"'}


# --- допустимые действия ----------------------------------------------------------

def emulation_actions(*, alive: bool, mode: str | None, can_start: bool) -> tuple[str, ...]:
    """Команды эмуляции, которые сервер примет сейчас.

    ``alive`` — процесс контура жив и ядро ведёт целевое состояние; ``mode`` —
    режим целевого состояния; ``can_start`` — процесса нет, ядро готово и ждёт команду.
    """
    if alive:
        if mode in (None, MODE_ON):
            return ("stop", "stop-close-all", "add-instruments")
        return ("stop-close-all",) if mode == NO_ENTRIES else ()
    return ("start",) if can_start else ()


def member_actions(*, changeable: bool, active: bool, remove_mode: str | None) -> tuple[str, ...]:
    """Команды одного участника; ``changeable`` — эмуляция принимает команды состава."""
    if not changeable:
        return ()
    if active:
        return ("remove", "remove-close-all")
    return ("remove-close-all", "restore") if remove_mode == NO_ENTRIES else ("restore",)


def run_actions(state: str) -> tuple[str, ...]:
    """Команды прогона истории: из очереди — убрать, идущий — остановить."""
    if state == "queued":
        return ("cancel",)
    return ("stop",) if state in ("starting", "running") else ()


def _action(name: str, title: str, method: str, href: str, body: dict | None = None) -> dict:
    action = {"name": name, "title": title, "method": method, "href": href}
    if body is not None:
        action["body"] = body
    return action


def emulation_action_list(names) -> list[dict]:
    table = {
        "start": ("Запустить эмуляцию", "PUT", EMULATION_TARGET, {"mode": MODE_ON}),
        "stop": ("Остановить, сделки доживают", "PUT", EMULATION_TARGET, {"mode": NO_ENTRIES}),
        "stop-close-all": ("Остановить и закрыть всё", "PUT", EMULATION_TARGET, {"mode": CLOSE_ALL}),
        "add-instruments": ("Добавить инструменты", "POST", EMULATION_INSTRUMENTS, {"instruments": []}),
    }
    return [_action(name, *table[name]) for name in names]


def member_action_list(name: str, names) -> list[dict]:
    table = {
        "remove": ("Убрать, сделки доживают", "POST", EMULATION_REMOVE, {"instruments": [name], "mode": NO_ENTRIES}),
        "remove-close-all": ("Убрать и закрыть позиции", "POST", EMULATION_REMOVE,
                             {"instruments": [name], "mode": CLOSE_ALL}),
        "restore": ("Вернуть в торговлю", "POST", EMULATION_INSTRUMENTS, {"instruments": [name]}),
    }
    return [_action(action, *table[action]) for action in names]


def sandbox_actions(*, alive: bool, mode: str | None, can_start: bool) -> tuple[str, ...]:
    """Команды песочницы, которые сервер примет сейчас; состав меняется и без процесса."""
    if alive:
        if mode in (None, MODE_ON):
            return ("stop", "stop-close-all", "add-bindings")
        return ("stop-close-all", "add-bindings") if mode == NO_ENTRIES else ("add-bindings",)
    return ("start", "add-bindings") if can_start else ("add-bindings",)


def sandbox_action_list(names) -> list[dict]:
    table = {
        "start": ("Запустить песочницу", "PUT", SANDBOX_TARGET, {"mode": MODE_ON}),
        "stop": ("Остановить", "PUT", SANDBOX_TARGET, {"mode": NO_ENTRIES}),
        "stop-close-all": ("Остановить, закрыть свои сделки", "PUT", SANDBOX_TARGET, {"mode": CLOSE_ALL}),
        "add-bindings": ("Добавить привязки", "POST", SANDBOX_BINDINGS, {"bindings": []}),
    }
    return [_action(name, *table[name]) for name in names]


def binding_action_list(binding: str, active: bool) -> list[dict]:
    if active:
        return [_action("remove", "Убрать из песочницы", "POST", SANDBOX_REMOVE,
                        {"bindings": [binding], "mode": NO_ENTRIES})]
    return [_action("restore", "Вернуть в песочницу", "POST", SANDBOX_BINDINGS, {"bindings": [binding]})]


def run_action_list(run_id: str, names) -> list[dict]:
    table = {"cancel": "Убрать из очереди", "stop": "Остановить с отчётом"}
    return [_action(name, table[name], "POST", run_href(run_id) + "/stop") for name in names]


# --- ошибки ------------------------------------------------------------------------

def problem(status: int, code: str, detail: str, extra: dict | None = None) -> dict:
    """Тело ``application/problem+json``; из ``extra`` берутся только ``rejections``, ``core``, ``pid`` и ``suggestions``."""
    body = {"type": PROBLEM_TYPE + code.lower().replace("_", "-"), "title": PROBLEM_TITLES.get(code, code),
            "status": int(status), "code": code, "detail": detail}
    extra = extra or {}
    if extra.get("rejections") is not None:
        body["rejections"] = [{"binding": item["binding"], "reason": item.get("reason")} if "binding" in item else
                              {"instrument": item.get("instrument", item.get("token")), "reason": item.get("reason")}
                              for item in extra["rejections"]]
    if isinstance(extra.get("core"), dict):
        body["core"] = {name: extra["core"].get(name) for name in _CORE_BRIEF}
    if extra.get("pid") is not None:
        body["pid"] = extra["pid"]
    if extra.get("suggestions") is not None:
        body["suggestions"] = [str(item) for item in extra["suggestions"]]
    return body
