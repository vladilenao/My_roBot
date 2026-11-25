"""Описание контуров на брокере (refactor-broker-port, решение 6; add-finam-contour, решение 12).

Контур на брокере собирается по описанию: номер, вид, отпечаток брокера, брокер адаптера, адаптер,
заголовок карточек, переменные окружения токена и номера счёта, умеет ли адаптер заявки и значения
настроек по умолчанию. Новый экземпляр вида «счёт» — новое описание и адаптер, без копии функций запуска
и без веток по номеру контура в общем коде, конфиге и API. Контуры работают одновременно, у каждого свой
процесс, каталог ``data/contours/<номер>/``, база, отпечаток счёта и канал.

Описание — данные: адаптеры брокеров здесь не импортируются, их выбирает точка сборки ``run.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

SANDBOX = "sandbox"
ACCOUNT = "account"        # вид «счёт»: брокерский счёт робота (add-account-contour)
FINAM = "finam"            # «Счёт Финам» (add-finam-contour)
TINVEST = "tinvest"        # брокер адаптера Т-Инвест


@dataclass(frozen=True)
class LiveContour:
    contour_id: str             # номер контура: папка, ключи [contours.<номер>], спрос ядра
    kind: str                   # вид: sandbox · account
    broker: str                 # отпечаток счёта в broker.sqlite3 (не менять у старых контуров)
    adapter: str                # адаптер брокера для точки сборки: tinvest-sandbox · tinvest-account · finam-account
    title: str                  # метка карточек в канале и консоли контура
    token_env: str              # переменная окружения токена брокера
    account_id_env: str | None = None   # переменная окружения номера счёта; у песочницы нет
    provider: str = TINVEST     # брокер адаптера: status.json и API «broker»
    trading: bool = True        # адаптер умеет заявки; нет — [contours.<номер>] orders = true отклоняется
    # Значения [contours.<номер>] по умолчанию, свои у контура: имена переменных бота и канала, orders.
    defaults: Mapping = field(default_factory=dict)
    name: str = ""              # название для человека в API и UI: «Счёт», «Счёт Финам», «Песочница»
    # Системный DNS-резолвер gRPC процесса контура (GRPC_DNS_RESOLVER=native, src/grpc_fork.py): c-ares не
    # пользуется системным DNS и терял api.finam.ru при сбое одного DNS-сервера (разведка Ф0, 25.11).
    native_dns: bool = False


ACCOUNT_CONTOUR = LiveContour(
    ACCOUNT, ACCOUNT, ACCOUNT, "tinvest-account", "СЧЁТ", "TINKOFF_TRADE_TOKEN", "TINKOFF_ACCOUNT_ID",
    defaults={"telegram_bot_env": "TELEGRAM_ACCOUNT_BOT_TOKEN", "telegram_channel_env": "TELEGRAM_ACCOUNT_CHANNEL_ID",
              "orders": False}, name="Счёт")
# Финам только читает счёт до Ф3: адаптер без торговли (add-finam-contour, решение 3).
FINAM_CONTOUR = LiveContour(
    FINAM, ACCOUNT, FINAM, "finam-account", "СЧЁТ ФИНАМ", "FINAM_SECRET_TOKEN", "FINAM_ACCOUNT_ID",
    provider=FINAM, trading=False,
    defaults={"telegram_bot_env": "TELEGRAM_FINAM_BOT_TOKEN", "telegram_channel_env": "TELEGRAM_FINAM_CHANNEL_ID",
              "orders": False}, name="Счёт Финам", native_dns=True)
SANDBOX_CONTOUR = LiveContour(
    SANDBOX, SANDBOX, SANDBOX, "tinvest-sandbox", "ПЕСОЧНИЦА", "TINKOFF_SANDBOX_TOKEN",
    defaults={"telegram_bot_env": "TELEGRAM_ACCOUNT_BOT_TOKEN", "telegram_channel_env": "TELEGRAM_SANDBOX_CHANNEL_ID",
              "orders": True}, name="Песочница")

# Порядок — порядок подъёма при старте ядра: «Счёт», «Счёт Финам», затем песочница.
LIVE_CONTOURS: tuple[LiveContour, ...] = (ACCOUNT_CONTOUR, FINAM_CONTOUR, SANDBOX_CONTOUR)
CONTOUR_TITLES = {contour.contour_id: contour.title for contour in LIVE_CONTOURS}


def live_contour(contour_id: str) -> LiveContour | None:
    """Описание контура на брокере по номеру; ``None`` — такого контура на брокере нет."""
    return next((contour for contour in LIVE_CONTOURS if contour.contour_id == contour_id), None)
