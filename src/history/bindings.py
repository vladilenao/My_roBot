"""Связки прогона истории (add-history-preflight-api).

Связка — привязка ``[strategies.*]``, по которой торгует инструмент прогона:
``id``, инструмент, стратегия, таймфрейм, фильтр и управление. Подбор — как у
торговли (``instrument_assignments``): акция по тикеру, фьючерс по семейству,
а если по семейству привязок нет — по его коду.

Инструмент связки — тикер акции, короткое имя фиксированного контракта или код
семейства автоматической цепочки: контракты цепочки меняются по ходу прогона.
"""

from __future__ import annotations

from src.history.preflight import instrument_assignments
from src.instruments.futures import FAMILIES


def binding_row(instrument: str, assignment) -> dict:
    """Связка в ответе API и в ``report.json``."""
    return {"id": assignment.id, "instrument": instrument, "strategy": str(assignment.strategy),
            "timeframe": assignment.timeframe, "filter": assignment.filter_profile,
            "management": assignment.management}


def family_assignments(family: str, future_strategies: dict) -> list:
    """Привязки семейства — по тем же правилам, что у контракта семейства в торговле."""
    if family in FAMILIES:
        return list(future_strategies.get(family, future_strategies.get(FAMILIES[family][0], [])))
    return list(future_strategies.get(family, []))


def _share_assignments(ticker: str, share_strategies: dict) -> list:
    found = share_strategies.get(ticker)
    if found is None:
        found = next((items for key, items in share_strategies.items() if key.upper() == ticker.upper()), [])
    return list(found)


def target_bindings(targets, share_strategies: dict, future_strategies: dict) -> list[dict]:
    """Связки по целям ``classify_tokens`` — при постановке прогона, без обращения к эмулятору."""
    rows: list[dict] = []
    for target in targets:
        if target.kind == "share":
            label, items = target.value, _share_assignments(target.value, share_strategies)
        else:
            label = target.family if target.kind == "family" else target.token
            items = family_assignments(target.family, future_strategies)
        rows.extend(binding_row(label, item) for item in items)
    return rows


def selection_bindings(instruments, share_strategies: dict, future_strategies: dict) -> list[dict]:
    """Связки выбранных инструментов прогона: фиксированные инструменты и семейства цепочки."""
    fixed = getattr(instruments, "fixed", None)
    families = getattr(instruments, "families", ())
    if fixed is None:
        fixed, families = tuple(instruments or ()), ()
    rows: list[dict] = []
    for instrument in fixed:
        # Биржевой тикер контракта пользователю не показывается (правило именования openspec/config.yaml).
        label = instrument.ticker if instrument.instrument_type == "share" else (
            getattr(instrument, "short_name", None) or "контракт не указан")
        rows.extend(binding_row(label, item)
                    for item in instrument_assignments(instrument, share_strategies, future_strategies))
    for family in families:
        rows.extend(binding_row(family, item) for item in family_assignments(family, future_strategies))
    return rows
