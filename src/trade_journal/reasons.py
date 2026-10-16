"""Русские подписи кодов причин: общий словарь журнала сделок и Telegram."""

from __future__ import annotations

REASON_LABELS = {
    "levels-profitable-add": "прибыльный добор по уровням",
    "atr-profitable-advance": "добор после продвижения по ATR",
    "ma-cloud-profitable-retest": "прибыльный ретест облака",
    "pattern-profitable-same-formation": "добор по той же формации",
    "cost-aware-break-even": "безубыток всей сделки",
    "atr-trailing-stop": "трейлинг по ATR",
    "ma-cloud-protective-stop": "подтяжка за облаком MA",
    "STOP": "стоп", "CLOSE": "закрытие", "REDUCE": "частичный выход",
    "next-bar": "вход перенесён на следующий бар",
    "entry-timeout": "вход не исполнен в отведённое время",
    "entry-undelivered": "вход не доставлен: данные не подтверждены вовремя",
    "contract-expiring": "контракт истекает",
    "contract-rollover": "переход на следующий контракт",
    "intraday-session-end": "закрытие перед концом торговой сессии",
    "intraday-carry-break-even": "безубыток для переноса через перерыв",
    "intraday-session-closed": "вход вне разрешённой сессии",
    "intraday-close-near": "слишком близко к концу сессии",
    "session-schedule-unknown": "расписание торгов неизвестно",
    "profile-entry": "вход по профилю",
    "protective": "стоп",
    "tp": "цель по цене",
    "risk-cap": "лимит риска",
    "duplicate-signal": "дублирующий сигнал",
    "opposite-exposure": "встречная позиция",
    "opposite-signal-management": "управление по встречному сигналу",
    "raw-opposite-signal": "встречный сигнал",
    "ma40-opposite-close": "цена ушла за MA40",
    "ma10-or-cloud-partial-exit": "частичный выход по MA10/облаку",
    "factual-increase-fill-invalidates-protection": "добор нарушил защиту",
    "factual-increase-fill-violates-risk": "добор нарушил лимиты риска",
    "late-increase-fill-after-cancel": "компенсация позднего добора",
    "clearing": "клиринг",
    "ttl": "истёк срок заявки",
    "source-absent-minute": "без стопа, тейка и переоценки",
    "late-candle": "исполнение не переигрывается",
    "late-candle-touched-stop": "свеча могла задеть стоп; исполнение не переигрывается",
    "late-candle-touched-target": "свеча могла задеть цель; исполнение не переигрывается",
    "close-not-confirmed": "закрытие не подтверждено",
    "filter-rejected": "сигнал отфильтрован",
    "no-contract-meta": "нет метаданных контракта",
    "insufficient-history": "недостаточно истории",
    "stale-state-revision": "устарела ревизия состояния",
    "trade-not-open": "позиция не открыта",
    "trade-already-open": "позиция уже открыта",
    "unknown-trade": "неизвестная сделка",
    "unknown-target": "неизвестная цель",
    "limit-entry-required": "вход только по лимитной заявке",
    "quantity-non-positive": "недопустимое количество",
    "quantity-exceeds-trade-remainder": "количество больше остатка позиции",
    "quantity-exceeds-target-remainder": "количество больше остатка цели",
    "unsupported-command": "неподдерживаемая команда",
}


def reason_label(code: object) -> str:
    """Перевести код причины в русскую фразу; неизвестный код — как есть."""
    text = str(code or "")
    if text.startswith("tp:"):
        return REASON_LABELS["tp"]
    return REASON_LABELS.get(text, text)
