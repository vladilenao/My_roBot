"""Имена инструментов разведки Финама (add-finam-probe, решение 4).

Имя задаётся как в роботе: тикер акции, короткое имя контракта (``NG-12.26``)
или код семейства (``NG`` → ближайший контракт). Фьючерс разрешается через
справочник Т-Инвест на ``TINKOFF_TOKEN`` (только чтение) — так же получается
карточка Т-Инвест для сравнения. Символ Финама ищется в ``Assets`` по биржевому
тикеру. Биржевой тикер фьючерса и полный ``symbol`` Финама человеку не
показываются: в выводе короткое имя и площадка.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from t_tech.invest.schemas import InstrumentIdType

from src.finam.gateway import FinamGateway
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2 as assets_pb2
from src.instruments.selector import active_futures_with_expiry
from src.sandbox.gateway import BrokerCallError, BrokerGateway, _account_client
from src.sandbox.instruments import (
    _CONTRACT_TICKER,
    InstrumentNameError,
    SandboxInstrument,
    _family,
    _Service,
    resolve_instrument,
)

__all__ = ["FinamInstrument", "InstrumentNameError", "Instruments", "ReferenceGateway", "dec"]

UNKNOWN_NAME = "инструмент не указан"
# Площадки Московской биржи — им отдаётся предпочтение, если тикер встретился на нескольких.
MOEX_MICS = ("MISX", "RTSX")
NO_REFERENCE = ("{name}: для фьючерса нужен TINKOFF_TOKEN — по нему разведка находит контракт и карточку "
                "Т-Инвест. Без него принимаются только акции.")


def dec(value) -> Decimal | None:
    """``google.type.Decimal`` (или ``Money``) → ``Decimal``; пустое поле — ``None``."""
    if value is None:
        return None
    text = getattr(value, "value", None)
    if text:
        return Decimal(text)
    units = getattr(value, "units", None)
    if units is not None and hasattr(value, "nanos"):
        return Decimal(units) + Decimal(value.nanos) / Decimal(10**9)
    return None


class ReferenceGateway(BrokerGateway):
    """Справочник Т-Инвест на ``TINKOFF_TOKEN``: только ``instruments.*`` и ``market_data.*``."""

    LABEL = "Справочник Т-Инвест"
    TOKEN_ENV = "TINKOFF_TOKEN"
    _default_factory = staticmethod(_account_client)
    ALLOWED = ("instruments", "market_data")

    def _check(self, method: str) -> None:
        if method.split(".", 1)[0] not in self.ALLOWED:
            raise BrokerCallError(method, "READ_ONLY", "справочнику доступно только чтение")


@dataclass
class FinamInstrument:
    """Инструмент разведки: короткое имя для человека, символ Финама для API, карточки обоих брокеров."""

    name: str
    kind: str  # share | future
    ticker: str  # биржевой тикер — только для API и записей
    symbol: str = ""  # символ Финама — только для API и записей
    mic: str = ""
    candidates: list = field(default_factory=list)
    tinvest: SandboxInstrument | None = None
    tinvest_go_buy: Decimal | None = None
    tinvest_go_sell: Decimal | None = None
    chosen: str | None = None
    asset: Any = None  # GetAssetResponse
    params: Any = None  # GetAssetParamsResponse

    @property
    def found(self) -> bool:
        return bool(self.symbol)

    @property
    def lot_size(self) -> Decimal | None:
        return dec(self.asset.lot_size) if self.asset is not None and self.asset.HasField("lot_size") else None

    @property
    def price_step(self) -> Decimal | None:
        """Шаг цены Финама: ``min_step / 10^decimals``, иначе ``price_step.step``."""
        if self.asset is None:
            return None
        if self.asset.min_step:
            return Decimal(self.asset.min_step).scaleb(-self.asset.decimals)
        if self.asset.HasField("price_step"):
            return dec(self.asset.price_step.step)
        return None


class Instruments:
    """Разрешение имён и подписи символов в рамках одного запуска."""

    def __init__(self, finam: FinamGateway, reference: BrokerGateway | None, *, now: datetime | None = None):
        self.finam = finam
        self.reference = reference
        self.now = now
        self._assets: list | None = None
        self._futures: list | None = None
        self._names: dict[str, str] = {}

    # --- справочники ------------------------------------------------------
    def assets(self) -> list:
        if self._assets is None:
            response = self.finam.call("assets.Assets", assets_pb2.AssetsRequest(),
                                       summarize=lambda r: {"assets_count": len(r.assets)})
            self._assets = list(response.assets)
        return self._assets

    def futures(self) -> list:
        if self._futures is None:
            if self.reference is None:
                self._futures = []
            else:
                now = self.now or datetime.now(timezone.utc).replace(tzinfo=None)
                self._futures = list(active_futures_with_expiry(_Service(self.reference), now))
        return self._futures

    # --- имя → инструмент -------------------------------------------------
    def resolve(self, name: str) -> FinamInstrument:
        token = name.strip().upper()
        if not token:
            raise InstrumentNameError("Не указан инструмент.")
        is_share = _family(token) is None and "-" not in token and not _CONTRACT_TICKER.fullmatch(token)
        if self.reference is None:
            if not is_share:
                raise InstrumentNameError(NO_REFERENCE.format(name=token))
            item = FinamInstrument(name=token, kind="share", ticker=token)
        else:
            card = resolve_instrument(self.reference, token, now=self.now)
            item = FinamInstrument(name=card.short_name, kind=card.kind, ticker=self._ticker(card),
                                   tinvest=card, chosen=card.chosen)
            if card.kind == "future":
                margin = self.reference.call("instruments.get_futures_margin", instrument_id=card.uid)
                item.tinvest_go_buy = _quotation(margin.initial_margin_on_buy)
                item.tinvest_go_sell = _quotation(margin.initial_margin_on_sell)
        self._find_symbol(item)
        return item

    def _ticker(self, card: SandboxInstrument) -> str:
        if card.kind == "share":
            return card.short_name
        entry = next((item for item in self.futures() if str(item[3]).upper() == card.short_name.upper()), None)
        if entry is not None:
            return str(entry[1])
        return self.reference.call("instruments.get_instrument_by", id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                                   id=card.uid).instrument.ticker

    def _find_symbol(self, item: FinamInstrument) -> None:
        ticker = item.ticker.upper()
        candidates = [asset for asset in self.assets() if asset.ticker.upper() == ticker and not asset.is_archived]
        item.candidates = [{"symbol": asset.symbol, "mic": asset.mic, "type": asset.type} for asset in candidates]
        if self.finam.recorder is not None:
            self.finam.recorder.write({"time": self.finam.clock().isoformat(), "method": "assets.Assets/поиск",
                                       "request": {"ticker": item.ticker}, "response": item.candidates})
        if not candidates:
            return
        moex = [asset for asset in candidates if asset.mic in MOEX_MICS]
        chosen = (moex or candidates)[0]
        item.symbol, item.mic = chosen.symbol, chosen.mic
        self._names[chosen.symbol] = item.name

    def remember(self, item: FinamInstrument) -> None:
        if item.symbol:
            self._names[item.symbol] = item.name

    # --- символ → имя для человека ----------------------------------------
    def name_of(self, symbol: str) -> str:
        """Короткое имя по символу Финама; фьючерс без имени — заглушка, не тикер."""
        if symbol in self._names:
            return self._names[symbol]
        ticker, _, mic = symbol.partition("@")
        entry = next((item for item in self.futures() if str(item[1]).upper() == ticker.upper()), None)
        if entry is not None:
            name = str(entry[3])
        elif mic == "MISX" and self._is_share(symbol):
            name = ticker
        else:
            name = UNKNOWN_NAME
        self._names[symbol] = name
        return name

    def _is_share(self, symbol: str) -> bool:
        if self._assets is None:
            return False
        asset = next((item for item in self._assets if item.symbol == symbol), None)
        kind = (asset.type if asset is not None else "").upper()
        return any(word in kind for word in ("EQUIT", "SHARE", "STOCK"))


def _quotation(value) -> Decimal | None:
    if not hasattr(value, "units"):  # незаданное поле SDK Т-Инвест — заглушка, а не Quotation
        return None
    return Decimal(value.units) + Decimal(value.nano) / Decimal(10**9)
