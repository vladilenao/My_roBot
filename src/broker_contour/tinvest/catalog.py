"""Каталог карточек ядра на Т-Инвест (add-finam-contour, решение 4): порт ``InstrumentCatalog``.

Карточка ядра — карточка Т-Инвест: по ней ядро строит свечи и сигналы, а журнал хранит её UID. Контур на
другом брокере получает каталог от точки сборки ``run.py`` и сам к Т-Инвест не ходит. Шлюз — справочник
на ``TINKOFF_TOKEN``, только ``instruments.*``. Короткое имя — правилом снимка Т-Инвест (``card_short_name``).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager

from t_tech.invest.schemas import InstrumentIdType
from t_tech.invest.utils import quotation_to_decimal

from src.broker_contour.port import BrokerCallError, InstrumentCard
from src.broker_contour.tinvest.gateway import ReferenceGateway
from src.sandbox.instruments import card_short_name

SHARE, FUTURE = "share", "future"
CLASS_CODES = {SHARE: "TQBR", FUTURE: "SPBFUT"}


def _card(instrument) -> InstrumentCard:
    kind = SHARE if instrument.instrument_type == "share" else FUTURE
    return InstrumentCard(uid=instrument.uid, ticker=instrument.ticker, kind=kind,
                          short_name=card_short_name(instrument), lot=int(instrument.lot),
                          price_step=quotation_to_decimal(instrument.min_price_increment))


class TInvestCatalog:
    """Карточки по UID и по биржевому тикеру с кэшем на процесс; ``NOT_FOUND`` — ``None``."""

    def __init__(self, gateway):
        self._gateway = gateway
        self._by_uid: dict[str, InstrumentCard | None] = {}
        self._by_ticker: dict[tuple[str, str], InstrumentCard | None] = {}

    def by_uid(self, uid: str) -> InstrumentCard | None:
        if uid not in self._by_uid:
            card = self._find(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid)
            self._remember(card)
            self._by_uid.setdefault(uid, card)
        return self._by_uid[uid]

    def by_ticker(self, ticker: str, kind: str) -> InstrumentCard | None:
        key = (ticker.upper(), kind)
        if key not in self._by_ticker:
            card = self._find(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER, class_code=CLASS_CODES[kind],
                              id=ticker)
            self._remember(card)
            self._by_ticker.setdefault(key, card)
        return self._by_ticker[key]

    def _remember(self, card: InstrumentCard | None) -> None:
        if card is not None:
            self._by_uid[card.uid] = card
            self._by_ticker[(card.ticker.upper(), card.kind)] = card

    def _find(self, **request) -> InstrumentCard | None:
        try:
            return _card(self._gateway.call("instruments.get_instrument_by", **request).instrument)
        except BrokerCallError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise


def catalog(token: str | None, *, gateway_factory: Callable = ReferenceGateway):
    """Фабрика открытого каталога для точки сборки: справочник Т-Инвест на токене ядра."""

    @contextmanager
    def open_catalog():
        with gateway_factory(token) as gateway:
            yield TInvestCatalog(gateway)

    return open_catalog
