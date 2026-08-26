## Why

Рефакторинг `src/strategies/` по Builder Pattern завершён. Текущая архитектура работает, но содержит结构性 проблемы: реестр, lazy loading и валидация смешаны в `__init__.py` (SRP), lazy loading хардкодит импорты стратегий (OCP), два модуля `base.py` с разным содержимым (source of confusion), отсутствуют type hints в функциях реестра, хрупкий круговой импорт между `__init__.py` и `macd_rsi_stoch.py`.

## What Changes

- **BREAKING**: `src/strategies/registry.py` — извлечение реестра из `__init__.py` (Single Responsibility): `register`, `_registry`, `_discover_strategies`, `get_strategy`, `all_strategies`, `strategy_names`, `validate_assignments`.
- **NEW**: Auto-discovery стратегий через `pkgutil.iter_modules` вместо хардкода импортов в `__init__.py` (Open/Closed).
- **BREAKING**: `src/strategies/base.py` → `src/strategies/contracts.py` — переименование для устранения двусмысленности (Naming Conventions): `Decision`, `SignalType`, `Strategy` Protocol.
- **BREAKING**: `src/strategies/__init__.py` — переписывается: только `__all__` и re-exports, вся логика вынесена в `registry.py`.
- **NEW**: Type hints для всех функций реестра (`register`, `get_strategy`, `all_strategies`, `strategy_names`, `validate_assignments`).
- **NEW**: `__all__` в `src/strategies/__init__.py`.
- **MODIFIED**: Обновление импортов во всех модулях, ссылающихся на `src.strategies.base`.

## Capabilities

### New Capabilities

- `strategy-registry`: Извлечение реестра стратегий в отдельный модуль `registry.py` с auto-discovery через `pkgutil.iter_modules`, type hints и полной документацией.

### Modified Capabilities

- `strategy-contract/spec.md`: Модуль `base.py` переименовывается в `contracts.py`. Путь к контрактам `Decision`, `SignalType`, `Strategy` Protocol изменяется. Требования к реестру переносятся в `strategy-registry`.
- `indicators/spec.md`: Без изменений (поведение индикаторов не меняется).

## Impact

- `src/strategies/registry.py` — создаётся (вся логика реестра)
- `src/strategies/contracts.py` — создаётся (вместо `base.py`)
- `src/strategies/base.py` — удаляется
- `src/strategies/__init__.py` — переписывается (только exports)
- `src/strategies/macd_rsi_stoch.py` — обновляется импорт
- `src/strategies/indicators/*.py` — обновляются импорты
- `tests/unit/test_registry.py` — обновляется импорт
- `tests/unit/test_strategy_builder.py` — обновляется импорт
