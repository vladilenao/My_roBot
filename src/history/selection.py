"""Выбор прогона сохраняет семейства отдельно от конкретных контрактов."""

from dataclasses import dataclass, replace
import json

from src.history.contract_chain import build_chain
from src.instruments.futures import FuturesCard, catalog_digest, family_for
from src.instruments.model import Instrument


@dataclass(frozen=True)
class HistorySelection:
    fixed: tuple[Instrument, ...] = ()
    families: tuple[str, ...] = ()
    catalog: tuple[FuturesCard, ...] = ()
    chains: tuple[tuple, ...] = ()

    def planned(self, start, end, settings, floor):
        for instrument in self.fixed:
            if instrument.instrument_type == "future" and family_for(instrument.ticker, instrument.short_name or "") in self.families:
                raise ValueError("Семейство нельзя выбирать одновременно автоматически и фиксированным контрактом")
        overrides = settings.get("families", {})
        chains = tuple(build_chain(self.catalog, family, start, end,
                                  overrides.get(family, {}).get("lead_days", floor))
                       for family in self.families)
        return replace(self, chains=chains)

    @property
    def instruments(self):
        return list(self.fixed)+[segment.card.instrument for chain in self.chains for segment in chain]

    def payload(self):
        return {"policy": "calendar", "position_policy": "close-and-wait",
                "families": self.families, "catalog_digest": catalog_digest(self.catalog),
                "catalog": [card.payload() for card in sorted(self.catalog, key=lambda c: c.uid)],
                "fixed": [{"contract": item.short_name, "instrument_type": item.instrument_type} for item in self.fixed],
                "segments": [segment.payload() for chain in self.chains for segment in chain],
                "economic_metadata": "static_catalog"}

    def save(self, directory):
        path = directory / "contract_chain.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.payload(), indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
