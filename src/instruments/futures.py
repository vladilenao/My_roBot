"""Неизменяемые карточки и идентичность исторических фьючерсов."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from src.instruments.model import Instrument

FAMILIES = {
    "RTS": ("RI", "Индекс РТС"), "BR": ("BR", "Нефть Brent"),
    "SI": ("SI", "Доллар — Рубль"), "SBRF": ("SR", "Сбер Банк"),
    "ED": ("ED", "Евро — Доллар"), "NG": ("NG", "Природный газ"),
    "GOLD": ("GD", "Золото"), "SILV": ("SV", "Серебро"),
}
_ALIASES = {alias: family for family, (prefix, _) in FAMILIES.items()
            for alias in (family, prefix)}


def family_key(value: str) -> str:
    try:
        return _ALIASES[value.upper()]
    except (KeyError, AttributeError) as exc:
        raise ValueError(f"Неизвестное семейство фьючерсов: {value!r}") from exc


def family_for(ticker: str, name: str = "", basic_asset: str = "") -> str | None:
    """Известные коды карточки; описательное basic_asset не считается кодом."""
    found = set()
    if basic_asset.upper() in _ALIASES:
        found.add(_ALIASES[basic_asset.upper()])
    short = name.split()[0].split("-")[0].upper() if name else ""
    if short in _ALIASES:
        found.add(_ALIASES[short])
    match = re.fullmatch(r"([A-Za-z]+)[FGHJKMNQUVXZ]\d{1,4}", ticker)
    code = match.group(1).upper() if match else ticker.split("-")[0].upper()
    if code in _ALIASES:
        found.add(_ALIASES[code])
    if len(found) > 1:
        raise ValueError("неоднозначное семейство карточки")
    return next(iter(found), None)


def lifecycle_time(value) -> datetime | None:
    """Только достоверный момент с поясом, без догадок о времени сессии."""
    if value is None or value == "":
        return None
    if not isinstance(value, (str, datetime)):
        raise ValueError("ожидается ISO 8601 с временем и часовым поясом")
    if isinstance(value, str):
        if "T" not in value and " " not in value:
            raise ValueError("дата без времени")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("момент без часового пояса")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class FuturesCard:
    uid: str
    ticker: str
    short_name: str | None
    family: str | None
    standard: bool
    first_trade_date: datetime | None
    expiration_date: datetime | None
    last_trade_date: datetime | None
    first_notice_date: datetime | None
    price_step: Decimal | None
    step_cost: Decimal | None
    go_buy: Decimal | None
    go_sell: Decimal | None
    basic_asset: str
    basic_asset_size: Decimal | None
    currency: str | None = None
    class_code: str | None = None
    errors: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: dict) -> FuturesCard:
        name = str(row.get("name") or "")
        ticker = str(row.get("ticker") or "")
        basic = str(row.get("basic_asset") or "")
        errors = []
        try:
            family = family_for(ticker, name, basic)
        except ValueError as exc:
            family = None
            errors.append(str(exc))
        dates = {}
        for key in ("first_trade_date", "expiration_date", "last_trade_date", "first_notice_date"):
            try:
                dates[key] = lifecycle_time(row.get(key))
            except (ValueError, OverflowError) as exc:
                dates[key] = None
                errors.append(f"{key}: {exc}")
        values = {}
        for field, key in (("price_step", "min_price_increment"),
                           ("step_cost", "min_price_increment_amount"),
                           ("go_buy", "initial_margin_on_buy"),
                           ("go_sell", "initial_margin_on_sell"),
                           ("basic_asset_size", "basic_asset_size")):
            raw = row.get(key)
            try:
                value = None if raw is None else Decimal(str(raw))
                if value is not None and (not value.is_finite() or value < 0):
                    raise ValueError("некорректное число")
                values[field] = value
            except (InvalidOperation, ValueError):
                values[field] = None
                errors.append(f"{key}: некорректное число")
        lower = name.lower()
        series = family_for(ticker) or family_for("", name)
        class_code = row.get("class_code") or None
        standard = series is not None and not any(word in lower for word in ("мини", "микро", "mini", "micro"))
        if class_code is not None and class_code != "SPBFUT":
            standard = False
        return cls(str(row.get("instrument_id") or ""), ticker, name.split()[0] if name else None,
                   family, standard, basic_asset=basic, currency=row.get("currency") or None,
                   class_code=class_code, errors=tuple(errors), **dates, **values)

    @property
    def instrument(self) -> Instrument:
        return Instrument(self.short_name or "контракт не указан", self.ticker, "future", self.short_name, self.uid, self.family)

    def problems(self) -> tuple[str, ...]:
        problems = list(self.errors)
        for key in ("uid", "ticker", "short_name", "family", "first_trade_date", "expiration_date",
                    "price_step", "step_cost", "go_buy", "go_sell"):
            if getattr(self, key) is None or getattr(self, key) == "":
                problems.append(f"отсутствует {key}")
        for key in ("price_step", "step_cost"):
            if getattr(self, key) is not None and getattr(self, key) <= 0:
                problems.append(f"{key} должен быть положительным")
        return tuple(problems)

    def payload(self) -> dict:
        return {key: value.isoformat()+"Z" if isinstance(value, datetime) else
                str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()}


def catalog_digest(cards) -> str:
    payload = sorted((card.payload() for card in cards), key=lambda row: (row["uid"], row["ticker"]))
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
