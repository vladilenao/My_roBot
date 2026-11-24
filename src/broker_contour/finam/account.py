"""Счёт робота у Финама (add-finam-contour, решения 9–10): правило счёта и снимок.

Правило: токен видит ровно один счёт, и это ``FINAM_ACCOUNT_ID``; права — по ``TokenDetails.readonly``.
Снимок — ``GetAccount`` и ``GetOrders`` по фактам разведки Ф0: у пустого счёта нет ``equity`` и ``cash``;
результат сделки приходит второй строкой ``cash``; закрытая позиция остаётся строкой с ``quantity`` 0;
на другой день ``GetOrders`` отдаёт только активные заявки. Полные номера наружу не выходят.
"""

from __future__ import annotations

from decimal import Decimal

from src.broker_contour.finam.symbols import FinamSymbols, SymbolMismatch
from src.broker_contour.finam.values import decimal_of, money_of
from src.broker_contour.port import AccountRuleError, AccountSnapshot, BrokerCallError, RobotAccount, mask_account
from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2 as accounts_pb2
from src.finam.proto.grpc.tradeapi.v1.auth import auth_service_pb2 as auth_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from src.logging_setup import get_logger, register_secrets

log = get_logger(__name__)

RUB = "RUB"
UNKNOWN_NAME = "инструмент не указан"
INCONSISTENT = "INCONSISTENT"       # как у Т-Инвест: снимок противоречив, сверка его не принимает
S = orders_pb2.OrderStatus
T = orders_pb2.OrderType
FINAL_STATUSES = frozenset({
    S.ORDER_STATUS_UNSPECIFIED, S.ORDER_STATUS_FILLED, S.ORDER_STATUS_CANCELED, S.ORDER_STATUS_REJECTED,
    S.ORDER_STATUS_EXPIRED, S.ORDER_STATUS_FAILED, S.ORDER_STATUS_DENIED_BY_BROKER,
    S.ORDER_STATUS_REJECTED_BY_EXCHANGE, S.ORDER_STATUS_EXECUTED, S.ORDER_STATUS_DISABLED,
    S.ORDER_STATUS_REPLACED, S.ORDER_STATUS_DONE_FOR_DAY, S.ORDER_STATUS_SL_EXECUTED, S.ORDER_STATUS_TP_EXECUTED,
})
STOP_TYPES = frozenset({T.ORDER_TYPE_STOP, T.ORDER_TYPE_STOP_LIMIT})
READ_ONLY, FULL_ACCESS = "read_only", "full_access"


def account_rule(transport, account_id: str) -> RobotAccount:
    """``TokenDetails``: счёт токена ровно один, его номер — ``account_id``; права — по ``readonly``."""
    register_secrets([account_id])
    details = transport.call("auth.TokenDetails", auth_pb2.TokenDetailsRequest(token=transport.jwt.token()))
    found = list(details.account_ids)
    register_secrets(found)                 # номера счетов токена — секреты до любой проверки
    if len(found) != 1:
        tails = ", ".join(mask_account(item) for item in found) or "—"
        raise AccountRuleError(f"Токен Финама видит счетов: {len(found)} ({tails}), а нужен ровно один — счёт "
                               "робота. Выпустите токен только на него.")
    if found[0] != account_id:
        raise AccountRuleError(f"Токен видит счёт {mask_account(found[0])}, а FINAM_ACCOUNT_ID задаёт другой")
    return RobotAccount(found[0], opened=False, access_level=READ_ONLY if details.readonly else FULL_ACCESS)


def _name(symbols: FinamSymbols, symbol: str, names: dict):
    """``(карточка ядра или None, короткое имя)``; ``names`` — UID → короткое имя, только по карточке."""
    card = symbols.card_for(symbol)
    if card is None:
        log.info("Финам: инструмент без карточки ядра, тикер %s", symbol.partition("@")[0])
        return None, UNKNOWN_NAME
    names[card.uid] = card.short_name
    return card, card.short_name


def _lots(card, symbol: str, quantity: Decimal, symbols: FinamSymbols) -> int:
    """Лоты позиции: фьючерс — контракты, акция — штуки ÷ лот карточки; нецелое — снимок противоречив."""
    if card is None:
        lots = quantity
    else:
        try:
            symbols.check(card, symbol)
        except SymbolMismatch as exc:
            log.warning("Финам: %s", exc)
        lots = quantity if card.kind == "future" else quantity / card.lot
    if lots != lots.to_integral_value():
        raise BrokerCallError("accounts.GetAccount", INCONSISTENT,
                              message="позиция счёта Финама — не целое число лотов")
    return int(lots)


def read_account(transport, symbols: FinamSymbols, account: RobotAccount, names: dict | None = None) -> AccountSnapshot:
    """Снимок счёта; сбой — ``BrokerCallError`` (окно обслуживания — ``BrokerOutage``) наружу."""
    names = {} if names is None else names
    response = transport.call("accounts.GetAccount", accounts_pb2.GetAccountRequest(account_id=account.account_id))
    orders = transport.call("orders.GetOrders", orders_pb2.OrdersRequest(account_id=account.account_id)).orders
    rub = sum((money_of(item) for item in response.cash if item.currency_code == RUB), Decimal(0))
    others = sum(1 for item in response.cash if item.currency_code != RUB)
    if others:
        log.info("Финам: строк денег не в рублях — %d, в капитал не входят", others)
    liquid = decimal_of(response.equity) if response.HasField("equity") else None
    holdings = []
    for position in response.positions:
        quantity = decimal_of(position.quantity) or Decimal(0)
        if quantity == 0:
            continue                         # закрытая позиция остаётся строкой с quantity 0
        card, name = _name(symbols, position.symbol, names)
        holdings.append((name, _lots(card, position.symbol, quantity, symbols)))
    active = [state for state in orders if state.status not in FINAL_STATUSES]
    plain = tuple(_name(symbols, state.order.symbol, names)[1] for state in active
                  if state.order.type not in STOP_TYPES)
    stops = tuple(_name(symbols, state.order.symbol, names)[1] for state in active if state.order.type in STOP_TYPES)
    return AccountSnapshot(rub, liquid, tuple(holdings), plain, stops, names)
