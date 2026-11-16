"""Имена инструментов команды песочницы: как в эмуляции, плюс код семейства.

Тикер акции и короткое имя контракта (``NG-12.26``) — как в эмуляции. Код
семейства (``NG``) для ручных действий разрешается в ближайший контракт по
правилу ближайшего контракта, а биржевой тикер фьючерса отклоняется.
Справочник берётся у песочницы тем же запросом, что меню выбора фьючерсов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from t_tech.invest.schemas import InstrumentIdType
from t_tech.invest.utils import quotation_to_decimal

from src.config import CONTRACT_EXPIRY_BLOCK_DAYS
from src.instruments.futures import family_for, family_key
from src.instruments.selector import active_futures_with_expiry
from src.sandbox.gateway import BrokerCallError, SandboxGateway

SHARE_CLASS_CODE = "TQBR"
FUTURES_CLASS_CODE = "SPBFUT"
_CONTRACT_TICKER = re.compile(r"[A-Za-z]+[FGHJKMNQUVXZ]\d{1,4}")


class InstrumentNameError(Exception):
    """Имя не разрешилось: сообщение для человека, код выхода 2."""


@dataclass(frozen=True)
class SandboxInstrument:
    short_name: str
    uid: str
    kind: str  # share | future
    lot: int
    price_step: Decimal
    step_cost: Decimal | None = None
    chosen: str | None = None  # «NG → NG-12.26, до экспирации 31 сутки»


class _Service:
    """``client.instruments.futures(...)`` для общих функций выбора через шлюз."""

    def __init__(self, gateway: SandboxGateway, prefix: str = ""):
        self._gateway = gateway
        self._prefix = prefix

    def __getattr__(self, name: str):
        if not self._prefix:
            return _Service(self._gateway, name)
        return lambda *args, **kwargs: self._gateway.call(f"{self._prefix}.{name}", *args, **kwargs)


def _days_word(days: int) -> str:
    return "сутки" if days % 10 == 1 and days % 100 != 11 else "суток"


def _family(token: str) -> str | None:
    try:
        return family_key(token)
    except ValueError:
        return None


def _entry_family(entry) -> str | None:
    try:
        return family_for(str(entry[1]), str(entry[3]))
    except ValueError:
        return None


def resolve_instrument(gateway: SandboxGateway, name: str, *, block_days: int = CONTRACT_EXPIRY_BLOCK_DAYS,
                       now: datetime | None = None) -> SandboxInstrument:
    token = name.strip().upper()
    if not token:
        raise InstrumentNameError("Не указан инструмент.")
    family = _family(token)
    if family is None and "-" not in token and not _CONTRACT_TICKER.fullmatch(token):
        return _share(gateway, token)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    futures = active_futures_with_expiry(_Service(gateway), now)
    chosen = None
    if family is not None:
        entry = next((item for item in futures if _entry_family(item) == family
                      and item[4] - now > timedelta(days=block_days)), None)
        if entry is None:
            raise InstrumentNameError(
                f"{token}: нет торгуемого контракта с экспирацией позже чем через {block_days} {_days_word(block_days)}.")
        days = int((entry[4] - now).total_seconds() // 86400)
        chosen = f"{token} → {entry[3]}, до экспирации {days} {_days_word(days)}"
    else:
        entry = next((item for item in futures if str(item[3]).upper() == token), None)
        if entry is None:
            by_ticker = next((item for item in futures if str(item[1]).upper() == token), None)
            if by_ticker is not None:
                raise InstrumentNameError(f"Биржевой тикер не принимается — используйте {by_ticker[3]}.")
            raise InstrumentNameError(f"{token}: контракт не найден среди торгуемых стандартных контрактов.")
    card = _card(gateway, entry[1], FUTURES_CLASS_CODE, entry[3])
    margin = gateway.call("instruments.get_futures_margin", instrument_id=card.uid)
    return SandboxInstrument(
        short_name=entry[3], uid=card.uid, kind="future", lot=card.lot,
        price_step=quotation_to_decimal(card.min_price_increment),
        step_cost=quotation_to_decimal(margin.min_price_increment_amount), chosen=chosen,
    )


def _share(gateway: SandboxGateway, ticker: str) -> SandboxInstrument:
    card = _card(gateway, ticker, SHARE_CLASS_CODE, ticker)
    return SandboxInstrument(short_name=ticker, uid=card.uid, kind="share", lot=card.lot,
                             price_step=quotation_to_decimal(card.min_price_increment))


def _card(gateway: SandboxGateway, ticker: str, class_code: str, shown: str):
    try:
        return gateway.call("instruments.get_instrument_by", id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                            class_code=class_code, id=ticker).instrument
    except BrokerCallError as exc:
        if exc.code == "NOT_FOUND":
            raise InstrumentNameError(f"{shown}: инструмент не найден у брокера.") from None
        raise


def short_name_by_uid(gateway: SandboxGateway, uid: str, cache: dict[str, str]) -> str:
    """Короткое имя позиции или заявки по UID; фьючерс без имени — заглушка, не тикер."""
    if uid in cache:
        return cache[uid]
    try:
        card = gateway.call("instruments.get_instrument_by", id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                            id=uid).instrument
    except BrokerCallError:
        cache[uid] = "контракт не указан"
        return cache[uid]
    if card.instrument_type == "share":
        name = card.ticker
    else:
        name = (card.name or "").split()[0] if card.name else "контракт не указан"
    cache[uid] = name or "контракт не указан"
    return cache[uid]
