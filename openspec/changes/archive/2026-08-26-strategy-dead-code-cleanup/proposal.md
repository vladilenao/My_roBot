## Why

Мёртвый код и冗余ные class attributes в `MacdRsiStochStrategy` создают путаницу:
- `EVENT_COLUMNS_TEMPLATE` определена, но нигде не используется
- `STRATEGY_CONFIGS` в `runner.py` дублирует `DEFAULT_CONFIG` из модуля стратегии, размывая ответственность
- Классные атрибуты `NAME`/`STRATEGY_WINDOW` объявлены, но всегда перезаписываются в `__init__`

## What Changes

- Удалить `EVENT_COLUMNS_TEMPLATE` из `macd_rsi_stoch.py`
- Убрать `STRATEGY_CONFIGS` из `runner.py`, импортировать `DEFAULT_CONFIG` из модуля стратегии
- Удалить классные атрибуты `NAME`/`STRATEGY_WINDOW` из `MacdRsiStochStrategy` (оставить только instance)

## Capabilities

### New Capabilities

_(нет)_

### Modified Capabilities

_(нет — поведение не меняется, только рефакторинг)_

## Impact

- `src/strategies/macd_rsi_stoch.py` — удаление мёртвого кода и классных атрибутов
- `src/scheduler/runner.py` — замена `STRATEGY_CONFIGS` на импорт `DEFAULT_CONFIG`
