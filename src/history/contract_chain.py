"""Чистое календарное планирование: свечи не участвуют в выборе контракта."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from src.instruments.futures import FuturesCard, family_key, family_for
from src.data.timeutil import to_naive


class ContractChainError(ValueError):
    """Историческая цепочка не может быть построена по достоверным данным."""


@dataclass(frozen=True)
class ChainSegment:
    card: FuturesCard
    start: datetime
    end: datetime
    cutoff: datetime
    deadline: datetime
    deadline_source: str
    lead_days: int

    def payload(self):
        return {"contract": self.card.short_name, "uid": self.card.uid,
                "family": self.card.family, "start": self.start.isoformat()+"Z",
                "end": self.end.isoformat()+"Z", "cutoff": self.cutoff.isoformat()+"Z",
                "deadline": self.deadline.isoformat()+"Z", "deadline_source": self.deadline_source,
                "lead_days": self.lead_days}


def contract_deadline(card: FuturesCard) -> tuple[datetime, str]:
    if card.expiration_date is None:
        raise ContractChainError(f"{card.short_name or 'контракт не указан'}: отсутствует expiration_date")
    boundaries = [(card.expiration_date, "expiration_only")]
    if card.last_trade_date is not None:
        # Tinkoff отдаёт expiration_date полуночью дня экспирации — это метка дня,
        # а не момент окончания торгов. Если в тот же день есть last_trade_date,
        # полночь отсекла бы последнюю сессию: при экспирации в понедельник
        # (BR-6.25, 2025-06-02) окно выхода целиком попадало на выходные.
        expiration = card.expiration_date
        if expiration.time() == time(0) and card.last_trade_date.date() == expiration.date():
            boundaries = []
        boundaries.append((card.last_trade_date, "last_trade_date"))
    if card.first_notice_date is not None:
        boundaries.append((card.first_notice_date, "first_notice_date"))
    moment, source = min(boundaries, key=lambda item: item[0])
    if source == "expiration_only" and len(boundaries) > 1:
        source = "expiration_date"
    return moment, source


def build_chain(cards, family: str, start, end, lead_days: int) -> tuple[ChainSegment, ...]:
    family = family_key(family)
    start, end = to_naive(start).to_pydatetime(), to_naive(end).to_pydatetime()
    if start >= end:
        raise ContractChainError("конец диапазона должен быть позже начала")
    if type(lead_days) is not int or lead_days < 0:
        raise ContractChainError("lead_days должен быть целым числом >= 0")
    for card in cards:
        if card.family is None and card.standard and card.errors:
            hints = {family_for(card.ticker), family_for("", card.short_name or ""), family_for("", "", card.basic_asset)}
            if family in hints and (card.expiration_date is None or card.expiration_date > start):
                raise ContractChainError(f"{card.short_name or 'контракт не указан'}: {'; '.join(card.errors)}")
    candidates = [card for card in cards if card.family == family and card.standard
                  and (card.expiration_date is None or card.expiration_date > start)]
    if not candidates:
        raise ContractChainError(f"{family}: в каталоге нет контрактов диапазона")
    for card in candidates:
        date_errors = []
        if card.expiration_date is None:
            date_errors.append("отсутствует expiration_date")
        if date_errors:
            raise ContractChainError(f"{card.short_name or 'контракт не указан'}: {'; '.join(date_errors)}")
    candidates.sort(key=lambda card: card.expiration_date)
    if len({card.expiration_date for card in candidates}) != len(candidates):
        raise ContractChainError(f"{family}: неоднозначные контракты с одинаковой экспирацией")
    if len({card.uid for card in candidates}) != len(candidates):
        raise ContractChainError(f"{family}: повторяющаяся идентичность контракта")
    schedule = []
    for card in candidates:
        deadline, source = contract_deadline(card)
        cutoff = deadline - timedelta(days=lead_days)
        schedule.append((card, cutoff, deadline, source))
    initial = None
    for i, (card, cutoff, _, _) in enumerate(schedule):
        if cutoff <= start:
            continue
        if card.first_trade_date is None:
            raise ContractChainError(f"{card.short_name}: отсутствует first_trade_date")
        if card.first_trade_date >= cutoff:
            raise ContractChainError(f"{card.short_name}: first_trade_date не раньше cutoff")
        if card.first_trade_date <= start:
            initial = i
            break
    if initial is None:
        raise ContractChainError(f"{family}: нет допустимого контракта на {start.isoformat()}")
    segments = []
    current = start
    for card, cutoff, deadline, source in schedule[initial:]:
        if card.problems():
            raise ContractChainError(f"{card.short_name or 'контракт не указан'}: {'; '.join(card.problems())}")
        if card.first_trade_date >= cutoff:
            raise ContractChainError(f"{card.short_name}: first_trade_date не раньше cutoff")
        if not card.first_trade_date <= current < cutoff:
            raise ContractChainError(f"{card.short_name}: следующий контракт недопустим на {current.isoformat()}")
        segment_end = min(cutoff, end)
        segments.append(ChainSegment(card, current, segment_end, cutoff, deadline, source, lead_days))
        current = segment_end
        if current == end:
            return tuple(segments)
    raise ContractChainError(f"{family}: отсутствует следующий контракт после {current.isoformat()}")
