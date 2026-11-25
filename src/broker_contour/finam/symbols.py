"""Инструменты Финама ↔ карточки ядра (add-finam-contour, решение 11).

Карточка ядра (``InstrumentCatalog``, Т-Инвест) знает биржевой тикер, вид, лот и шаг; Финам — ``symbol``
вида ``SBER@MISX``, фьючерсы на RTSX. Соответствие строится по тикеру и площадке и проверяется по шагу
и лоту: у фьючерса лот Финама — ``trade_lot_size`` из ``GetAssetParams`` (``lot_size`` — размер контракта),
у акции — ``lot_size``. ГО Финама своё и плавает — ``margin`` читает его при каждом вызове.
"""

from __future__ import annotations

from decimal import Decimal

from src.broker_contour.finam.values import decimal_of, money_of
from src.broker_contour.port import InstrumentCard, InstrumentCatalog
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2 as assets_pb2
from src.logging_setup import get_logger

log = get_logger(__name__)

SHARE, FUTURE = "share", "future"
MICS = {FUTURE: "RTSX", SHARE: "MISX"}
KINDS = {mic: kind for kind, mic in MICS.items()}


class SymbolMismatch(Exception):
    """Шаг или лот инструмента у Финама не совпал с карточкой ядра."""

    def __init__(self, short_name: str, field: str, ours, theirs):
        super().__init__(short_name, field, ours, theirs)
        self.short_name, self.field, self.ours, self.theirs = short_name, field, ours, theirs

    def __str__(self) -> str:
        return f"{self.short_name}: {self.field} у Финама {self.theirs}, в карточке ядра {self.ours}"


class FinamSymbols:
    def __init__(self, transport, catalog: InstrumentCatalog, account_id: str):
        self._transport, self._catalog, self._account_id = transport, catalog, account_id
        self._assets: list | None = None
        self._checked: dict[str, SymbolMismatch | None] = {}

    def _all_assets(self) -> list:
        if self._assets is None:
            self._assets = list(self._transport.call("assets.Assets", assets_pb2.AssetsRequest()).assets)
        return self._assets

    # --- соответствие ---------------------------------------------------------
    def symbol_for(self, card: InstrumentCard) -> str | None:
        """``symbol`` Финама для карточки ядра или ``None``; шаг или лот не совпали — ``SymbolMismatch``."""
        mic = MICS[card.kind]
        ticker = card.ticker.upper()
        found = next((asset for asset in self._all_assets()
                      if asset.ticker.upper() == ticker and asset.mic == mic and not asset.is_archived), None)
        if found is None:
            return None
        self.check(card, found.symbol)
        return found.symbol

    def card_for(self, symbol: str) -> InstrumentCard | None:
        """Карточка ядра по ``symbol`` Финама; площадка не Мосбиржи или карточки нет — ``None``."""
        ticker, _, mic = symbol.partition("@")
        kind = KINDS.get(mic)
        return None if kind is None or not ticker else self._catalog.by_ticker(ticker, kind)

    def check(self, card: InstrumentCard, symbol: str) -> None:
        """Шаг и лот Финама против карточки ядра; проверка — раз за процесс на ``symbol``."""
        if symbol not in self._checked:
            self._checked[symbol] = self._compare(card, symbol)
        problem = self._checked[symbol]
        if problem is not None:
            raise problem

    def _compare(self, card: InstrumentCard, symbol: str) -> SymbolMismatch | None:
        asset = self._transport.call("assets.GetAsset", assets_pb2.GetAssetRequest(symbol=symbol,
                                                                                    account_id=self._account_id))
        step = Decimal(asset.min_step).scaleb(-asset.decimals) if asset.min_step else None
        if step is None and asset.HasField("price_step"):
            step = decimal_of(asset.price_step.step)
        if step is None or step != card.price_step:
            return SymbolMismatch(card.short_name, "шаг", card.price_step, step)
        if card.kind == FUTURE:
            lot = self._params(symbol).trade_lot_size
        else:
            lot = decimal_of(asset.lot_size)
        if lot is None or Decimal(lot) != card.lot:
            return SymbolMismatch(card.short_name, "лот", card.lot, lot)
        return None

    # --- ГО -------------------------------------------------------------------
    def _params(self, symbol: str):
        return self._transport.call("assets.GetAssetParams", assets_pb2.GetAssetParamsRequest(
            symbol=symbol, account_id=self._account_id))

    def margin(self, symbol: str) -> dict[str, Decimal | None]:
        """ГО Финама на лонг и шорт сейчас: ``GetAssetParams`` при каждом вызове, без кэша."""
        params = self._params(symbol)
        return {"go_buy": money_of(params.long_initial_margin) if params.HasField("long_initial_margin") else None,
                "go_sell": money_of(params.short_initial_margin) if params.HasField("short_initial_margin") else None}
