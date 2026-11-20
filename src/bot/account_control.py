"""Ресурс API ``/api/v1/account``: контур «Счёт» на брокерском счёте робота.

Тот же ресурс, что у песочницы (add-account-contour), кроме: в ``current`` —
факты допуска; в состав принимаются только привязки из состава песочницы, а при
``orders = false`` команды состава отвечают ``409 ACCOUNT_READ_ONLY``
(enable-live-trading); ``close_all`` — остановка и снятие привязки — только с
``confirm: true``; отчёт сверки — вместе с разведкой комиссий.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from src.account.fee_probes import read_fee_probes
from src.assembly.core_state import CLOSE_ALL, STOP_MODES
from src.bot import control_views as views
from src.bot.launch_control import BadCommand
from src.bot.sandbox_control import MODES, SandboxControl, _ids
from src.broker_contour.store import BROKER_DATABASE_FILE

NOT_IN_SANDBOX = "Привязки нет в составе песочницы: путь эмуляция → песочница → счёт."


def _confirm(body: dict) -> bool:
    confirm = body.get("confirm", False)
    if not isinstance(confirm, bool):
        raise BadCommand("INVALID_REQUEST", "confirm — true или false.")
    return confirm


def parse_account_target(body) -> tuple[str, tuple[str, ...] | None, bool]:
    """Тело ``PUT /api/v1/account/target`` → ``(режим, привязки, подтверждение)``; привязки — только у ``on``."""
    if not isinstance(body, dict) or body.get("mode") not in MODES:
        raise BadCommand("INVALID_REQUEST", "Тело — {\"mode\": \"on\", \"bindings\": [...]}, {\"mode\": \"no_entries\"} "
                                            "или {\"mode\": \"close_all\", \"confirm\": true}.")
    unknown = set(body) - {"mode", "bindings", "confirm"}
    if unknown:
        raise BadCommand("INVALID_REQUEST", f"Неизвестные поля: {', '.join(sorted(unknown))}.")
    confirm = _confirm(body)
    if body["mode"] != views.MODE_ON:
        if "bindings" in body:
            raise BadCommand("INVALID_REQUEST", "bindings передаётся только с mode = on.")
        return body["mode"], None, confirm
    return views.MODE_ON, (_ids(body["bindings"], required=False) if "bindings" in body else None), confirm


def parse_account_bindings(body, *, remove: bool) -> tuple[tuple[str, ...], str, bool]:
    """Тело ``POST /api/v1/account/bindings[/remove]`` → ``(привязки, режим снятия, подтверждение)``."""
    allowed = {"bindings", "mode", "confirm"} if remove else {"bindings", "confirm"}
    if not isinstance(body, dict) or "bindings" not in body or set(body) - allowed:
        raise BadCommand("INVALID_REQUEST", "Тело — {\"bindings\": [...]}" +
                         (", \"mode\": \"no_entries\" | \"close_all\", \"confirm\": true" if remove else "") + ".")
    mode = body.get("mode", "no_entries")
    if mode not in STOP_MODES:
        raise BadCommand("INVALID_REQUEST", "mode — no_entries или close_all.")
    return _ids(body["bindings"], required=True), mode, _confirm(body)


class AccountControl(SandboxControl):
    TITLE = "Счёт"
    BASE = views.ACCOUNT
    TRADES_IN = "account"

    def __init__(self, *, orders_enabled: bool = False, sandbox_members: Callable[[], set[str]] = lambda: set(),
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.orders_enabled = orders_enabled
        self._sandbox_members = sandbox_members

    def _links(self) -> dict:
        extra = {"comparison": views.ACCOUNT_COMPARISON} if self.orders_enabled else {}
        return views.links(self=views.ACCOUNT, target=views.ACCOUNT_TARGET, bindings=views.ACCOUNT_BINDINGS,
                           reconcile=views.ACCOUNT_RECONCILE, log=views.ACCOUNT_LOG, **extra,
                           **views.trading_links(views.ACCOUNT))

    def _action_list(self, names) -> list[dict]:
        allowed = ("start", "stop", "stop-close-all") + (("add-bindings", "guards-reset") if self.orders_enabled else ())
        return views.account_action_list([name for name in names if name in allowed])

    @staticmethod
    def _current(status) -> dict | None:
        current = SandboxControl._current(status)
        if current is not None:
            current["admission"] = status.get("admission")
        return current

    def _member_view(self, member, status, running: bool) -> dict:
        view = super()._member_view(member, status, running)
        view["_actions"] = views.account_binding_actions(member.key, member.active) if self.orders_enabled else []
        return view

    def bindings(self) -> dict:
        payload = super().bindings()
        payload["_links"] = views.links(self=views.ACCOUNT_BINDINGS, account=views.ACCOUNT)
        payload["_actions"] = views.account_action_list(("add-bindings",)) if self.orders_enabled else []
        return payload

    def reconcile(self) -> dict | None:
        report = super().reconcile()
        if report is None:
            return None
        return {**report, "fee_probes": read_fee_probes(self.directory / BROKER_DATABASE_FILE)}

    # --- команды --------------------------------------------------------------
    def _read_only(self) -> tuple[int, dict]:
        return self._problem(HTTPStatus.CONFLICT, "ACCOUNT_READ_ONLY",
                             "Заявки на счёте выключены ([contours.account] orders = false): состав не меняется.")

    def _not_confirmed(self) -> tuple[int, dict]:
        return self._problem(HTTPStatus.CONFLICT, "CONFIRM_REQUIRED",
                             "close_all на настоящем счёте — только с \"confirm\": true.")

    def _outside_sandbox(self, ids) -> tuple[int, dict] | None:
        """Привязки, которых нет в составе песочницы (enable-live-trading, решение 3); неизвестные — общей проверкой."""
        rejected = self._rejected(ids)
        if rejected is not None:
            return rejected
        ours = self._sandbox_members()
        missing = [item for item in ids if item not in ours]
        if not missing:
            return None
        return self._rejected((), [(item, NOT_IN_SANDBOX) for item in missing])

    def put_account_target(self, mode: str, bindings, confirm: bool, expected: int | None) -> tuple[int, dict]:
        if mode == CLOSE_ALL and not confirm:
            return self._not_confirmed()
        if bindings is not None:
            if not self.orders_enabled:
                return self._read_only()
            refused = self._start_refused() or self._outside_sandbox(bindings)
            if refused is not None:
                return refused
        return self.put_target(mode, bindings, expected)

    def change_account_bindings(self, ids, *, remove: bool, mode: str, confirm: bool,
                                expected: int | None) -> tuple[int, dict]:
        if not self.orders_enabled:
            return self._read_only()
        if remove and mode == CLOSE_ALL and not confirm:
            return self._not_confirmed()
        if not remove:
            refused = self._outside_sandbox(ids)
            if refused is not None:
                return refused
        return self.change_bindings(ids, remove=remove, mode=mode, expected=expected)
