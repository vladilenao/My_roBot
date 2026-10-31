"""Привязки конфигурации как участники состава контура на брокере (add-sandbox-contour).

Участник — ``id`` привязки из ``[strategies.*]``; карточка — инструмент,
стратегия, управление, фильтр и таймфрейм. Состав хранится в
``contour_members`` ``core.sqlite3`` с ключом ``id``.
"""

from __future__ import annotations

from src.assembly.core_state import Member
from src.instruments.futures import family_for


def binding_catalog(share_strategies: dict, future_strategies: dict) -> dict[str, dict]:
    """Все привязки конфигурации: ``id`` → карточка, в порядке конфигурации."""
    catalog: dict[str, dict] = {}
    for instrument_type, strategies in (("share", share_strategies), ("future", future_strategies)):
        for instrument, assignments in strategies.items():
            for item in assignments:
                catalog[item.id] = {"id": item.id, "instrument": instrument, "instrument_type": instrument_type,
                                    "strategy": str(item.strategy), "management": item.management,
                                    "filter": item.filter_profile, "timeframe": item.timeframe}
    return catalog


def binding_member(card: dict, instrument: dict | None = None, contract: dict | None = None) -> Member:
    """Участник-привязка: карточка привязки и, если ядро определило, карточка инструмента и контракта
    (add-tinvest-orders). Поля карточки инструмента дописываются к карточке привязки."""
    return Member(card["id"], {**card, **(instrument or {})}, None, contract)


def resolved(member: Member) -> bool:
    """Инструмент привязки определён ядром: у карточки есть тикер, у фьючерса — и карточка контракта."""
    instrument = member.instrument or {}
    if not instrument.get("ticker"):
        return False
    return instrument.get("instrument_type") != "future" or bool(member.contract)


def unknown_bindings(ids, catalog: dict) -> list[tuple[str, str]]:
    """Отказы ``(id, причина)`` для привязок, которых нет в конфигурации."""
    return [(item, "Привязки нет в конфигурации.") for item in ids if item not in catalog]


def emulation_traded(card: dict, emulation_members) -> bool:
    """Инструмент привязки сейчас в составе эмуляции: акция — по тикеру, фьючерс — по семейству."""
    for member in emulation_members:
        if not member.active:
            continue
        ticker = str(member.instrument.get("ticker") or "")
        if card["instrument_type"] == "share" and ticker.upper() == card["instrument"].upper():
            return True
        if card["instrument_type"] == "future":
            try:
                family = family_for(ticker, member.name)
            except ValueError:
                family = None
            if family is not None and family.upper() == card["instrument"].upper():
                return True
    return False
