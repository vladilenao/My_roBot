## Context

`MacdRsiStochStrategy` содержит мёртвый код (`EVENT_COLUMNS_TEMPLATE`) и冗余ные class attributes (`NAME`/`STRATEGY_WINDOW`), которые всегда перезаписываются в `__init__`. `runner.py` дублирует конфиг стратегии в `STRATEGY_CONFIGS`, хотя `DEFAULT_CONFIG` уже определён в модуле стратегии.

## Goals / Non-Goals

**Goals:**
- Удалить мёртвый код
- Устранить дублирование конфигурации
- Убрать冗余ные class attributes

**Non-Goals:**
- Рефакторинг `expected_events` (отложено)
- Изменение поведения стратегии

## Decisions

1. **Источник дефолтного конфига** → `DEFAULT_CONFIG` в `macd_rsi_stoch.py`
   - Альтернатива: `STRATEGY_CONFIGS` в runner'е — отвергнута, т.к. размывает ответственность
   - Решение: runner импортирует `DEFAULT_CONFIG` из модуля стратегии

2. **Классные атрибуты** → удалить, оставить только instance
   - `Strategy` Protocol требует `NAME`/`STRATEGY_WINDOW` на instance, не на классе
   - Классные атрибуты всегда перезаписываются в `__init__` → бесполезны

3. **`EVENT_COLUMNS_TEMPLATE`** → удалить
   - Определена, но нигде не используется

## Risks / Trade-offs

- **Нет** — это чистый рефакторинг без изменения поведения
