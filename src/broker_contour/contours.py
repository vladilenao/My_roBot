"""Описание контуров на брокере (refactor-broker-port, design.md решение 6).

Контур на брокере собирается по описанию: номер, вид, отпечаток брокера, адаптер, заголовок карточек,
переменные окружения токена и номера счёта. Новый экземпляр вида «счёт» (Ф2: ``finam``) — новое
описание и адаптер, без копии функций запуска. Контуры работают одновременно, у каждого свой процесс,
каталог ``data/contours/<номер>/``, база, отпечаток счёта и канал.

Описание — данные: адаптеры брокеров здесь не импортируются, их выбирает точка сборки ``run.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

SANDBOX = "sandbox"
ACCOUNT = "account"        # вид «счёт»: брокерский счёт робота (add-account-contour)


@dataclass(frozen=True)
class LiveContour:
    contour_id: str             # номер контура: папка, ключи [contours.<номер>], спрос ядра
    kind: str                   # вид: sandbox · account
    broker: str                 # отпечаток счёта в broker.sqlite3
    adapter: str                # адаптер брокера для точки сборки: tinvest-sandbox · tinvest-account
    title: str                  # метка карточек в канале контура
    token_env: str              # переменная окружения токена брокера
    account_id_env: str | None = None   # переменная окружения номера счёта; у песочницы нет


ACCOUNT_CONTOUR = LiveContour(ACCOUNT, ACCOUNT, ACCOUNT, "tinvest-account", "СЧЁТ", "TINKOFF_TRADE_TOKEN",
                              "TINKOFF_ACCOUNT_ID")
SANDBOX_CONTOUR = LiveContour(SANDBOX, SANDBOX, SANDBOX, "tinvest-sandbox", "ПЕСОЧНИЦА", "TINKOFF_SANDBOX_TOKEN")

# Порядок — порядок подъёма при старте ядра: первым «Счёт» (add-account-contour), затем песочница.
LIVE_CONTOURS: tuple[LiveContour, ...] = (ACCOUNT_CONTOUR, SANDBOX_CONTOUR)
CONTOUR_TITLES = {contour.contour_id: contour.title for contour in LIVE_CONTOURS}


def live_contour(contour_id: str) -> LiveContour | None:
    """Описание контура на брокере по номеру; ``None`` — такого контура на брокере нет."""
    return next((contour for contour in LIVE_CONTOURS if contour.contour_id == contour_id), None)
