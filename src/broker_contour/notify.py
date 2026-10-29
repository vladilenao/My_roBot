"""Уведомления контура на брокере: события ``broker_account`` в свою шину и свой канал.

С заявками (add-tinvest-orders) канал контура получает и торговые события своего
контура — сигнал, карточку сделки с меткой контура, ``order_accepted`` и
``broker_order``; списки ``[notifier.telegram] events`` эмуляции на него не влияют.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from src.broker_contour.account import AccountSnapshot
from src.events.bus import EventBus
from src.events.event import Event
from src.events.types import TRADING_EVENT_TYPES, EventType

DELIVERY_FILE = "telegram_delivery.sqlite3"


CONTOUR_TITLES = {"sandbox": "ПЕСОЧНИЦА", "account": "СЧЁТ"}
BROKER_TYPES = frozenset({EventType.BROKER_ACCOUNT, EventType.BROKER_ORDER})
TRADING_TYPES = BROKER_TYPES | TRADING_EVENT_TYPES | {EventType.ORDER_ACCEPTED}
CONSOLE_TRADING_TYPES = TRADING_TYPES | {EventType.REJECTED, EventType.RISK_LIMIT_HIT, EventType.ERROR}


def build_channels(*, bot_token: str, channel_id: str, directory: Path, cloudflare_url: str | None,
                   tz_offset_hours: float, request_timeout: float, max_transport_attempts: int,
                   contour: str = "sandbox", orders: bool = False) -> list:
    """Консоль процесса и Telegram-канал контура: ``broker_account``, с заявками — и торговые события."""
    from src.notifier.console import ConsoleChannel
    from src.notifier.telegram import TelegramChannel

    types = TRADING_TYPES if orders else {EventType.BROKER_ACCOUNT}
    return [
        ConsoleChannel(supported_types=CONSOLE_TRADING_TYPES if orders else types),
        TelegramChannel(bot_token=bot_token, channel_id=channel_id, cloudflare_url=cloudflare_url,
                        tz_offset_hours=tz_offset_hours, supported_types=types, request_timeout=request_timeout,
                        max_transport_attempts=max_transport_attempts, delivery_path=Path(directory) / DELIVERY_FILE,
                        title=CONTOUR_TITLES.get(contour)),
    ]


class BrokerNotifier:
    def __init__(self, bus: EventBus, *, contour: str, account_tail: str = "…"):
        self._bus = bus
        self._contour = contour
        self.account_tail = account_tail

    def _publish(self, kind: str, **fields) -> None:
        self._bus.publish(Event.broker_account(kind, contour=self._contour, account=self.account_tail,
                                               event_id=str(uuid.uuid4()), **fields))

    def started(self, snapshot: AccountSnapshot, *, bindings: int, generation: int | None,
                orders_enabled: bool | None = None) -> None:
        self._publish("started", rub=snapshot.rub, liquid_portfolio=snapshot.liquid_portfolio,
                      holdings=[{"name": name, "lots": lots} for name, lots in snapshot.holdings],
                      orders=len(snapshot.orders), stop_orders=len(snapshot.stop_orders), bindings=bindings,
                      generation=generation, orders_enabled=orders_enabled)

    def alarm(self, items, frozen, *, reason: str | None = None) -> None:
        self._publish("reconcile_alarm", mismatches=list(items), frozen=list(frozen), reason=reason)

    def cleared(self, unfrozen) -> None:
        self._publish("reconcile_cleared", unfrozen=list(unfrozen))

    def stopped(self, reason: str, *, open_positions: int | None = None) -> None:
        self._publish("stopped", reason=reason, open_positions=open_positions)
