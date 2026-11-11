"""Ресурс API ``/api/v1/account`` (add-account-contour): контур «Счёт» на брокерском счёте робота.

Тот же ресурс, что у песочницы, кроме: в ``current`` — факты допуска, запуск без
состава, ``close_all`` только с ``confirm: true``, команды состава до Р2 отвечают
``409 ACCOUNT_READ_ONLY``, отчёт сверки — вместе с разведкой комиссий.
"""

from __future__ import annotations

from http import HTTPStatus

from src.account.fee_probes import read_fee_probes
from src.assembly.core_state import CLOSE_ALL
from src.bot import control_views as views
from src.bot.launch_control import BadCommand
from src.bot.sandbox_control import MODES, SandboxControl
from src.broker_contour.store import BROKER_DATABASE_FILE


def parse_account_target(body) -> tuple[str, bool]:
    """Тело ``PUT /api/v1/account/target`` → ``(режим, подтверждение)``; состава в теле нет."""
    if not isinstance(body, dict) or body.get("mode") not in MODES:
        raise BadCommand("INVALID_REQUEST", "Тело — {\"mode\": \"on\"}, {\"mode\": \"no_entries\"} или "
                                            "{\"mode\": \"close_all\", \"confirm\": true}.")
    if "bindings" in body:
        raise BadCommand("INVALID_REQUEST", "bindings у «Счёта» не передаётся: состав задаётся командами с Р2.")
    unknown = set(body) - {"mode", "confirm"}
    if unknown:
        raise BadCommand("INVALID_REQUEST", f"Неизвестные поля: {', '.join(sorted(unknown))}.")
    confirm = body.get("confirm", False)
    if not isinstance(confirm, bool):
        raise BadCommand("INVALID_REQUEST", "confirm — true или false.")
    return body["mode"], confirm


class AccountControl(SandboxControl):
    TITLE = "Счёт"
    BASE = views.ACCOUNT
    TRADES_IN = "account"

    def _links(self) -> dict:
        return views.links(self=views.ACCOUNT, target=views.ACCOUNT_TARGET, bindings=views.ACCOUNT_BINDINGS,
                           reconcile=views.ACCOUNT_RECONCILE, log=views.ACCOUNT_LOG,
                           **views.trading_links(views.ACCOUNT))

    def _action_list(self, names) -> list[dict]:
        # До Р2 у «Счёта» нет состава и предохранителей: остаются запуск и остановка.
        return views.account_action_list([name for name in names if name in ("start", "stop", "stop-close-all")])

    @staticmethod
    def _current(status) -> dict | None:
        current = SandboxControl._current(status)
        if current is not None:
            current["admission"] = status.get("admission")
        return current

    def bindings(self) -> dict:
        payload = super().bindings()
        payload["_links"] = views.links(self=views.ACCOUNT_BINDINGS, account=views.ACCOUNT)
        payload["_actions"] = []
        return payload

    def reconcile(self) -> dict | None:
        report = super().reconcile()
        if report is None:
            return None
        return {**report, "fee_probes": read_fee_probes(self.directory / BROKER_DATABASE_FILE)}

    def put_account_target(self, mode: str, confirm: bool, expected: int | None) -> tuple[int, dict]:
        if mode == CLOSE_ALL and not confirm:
            return self._problem(HTTPStatus.CONFLICT, "CONFIRM_REQUIRED",
                                 "close_all на настоящем счёте — только с \"confirm\": true.")
        return self.put_target(mode, None, expected)

    def change_bindings(self, ids, *, remove: bool, mode: str, expected: int | None) -> tuple[int, dict]:
        return self._problem(HTTPStatus.CONFLICT, "ACCOUNT_READ_ONLY",
                             "Состав «Счёта» меняется командами с Р2: до него контур только читает счёт.")

    def reset_guards(self, guards) -> tuple[int, dict]:
        return self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Предохранителей у «Счёта» до Р2 нет.")
