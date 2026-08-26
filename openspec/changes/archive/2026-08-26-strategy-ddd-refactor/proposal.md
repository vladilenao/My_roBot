## Why

Текущая архитектура стратегий нарушает принципы DDD: индикаторы существуют как отдельные модули без чёткой принадлежности к агрегату, параметры передаются через дефолты (неявно), Enum-ы типов сигналов индикаторов отсутствуют (сигналы — целые числа), а конфигурация стратегии спрятана в функцию `_build_default_config()`. Это приводит к неявности: невозможно понять параметры стратегии, не открывая внутренний код индикаторов.

## What Changes

- **BREAKING**: Убраны дефолтные значения параметров из всех индикаторов (`MacdIndicator`, `RsiIndicator`, `StochasticIndicator`) — параметры обязательны при создании.
- **BREAKING**: Убран fallback `config=None` из `MacdRsiStochStrategy.__init__` — конфигурация обязательна.
- **BREAKING**: Убраны Builder-ы индикаторов (`MacdIndicatorBuilder`, `RsiIndicatorBuilder`, `StochasticIndicatorBuilder`) — избыточны при explicit parameters.
- **BREAKING**: Изменена структура каталогов `indicators/` — каждый индикатор теперь в подпапке (`indicators/macd/`, `indicators/rsi/`, `indicators/stochastic/`).
- **BREAKING**: Добавлены Enum-ы типов сигналов для каждого индикатора (`MacdSignalType`, `RsiSignalType`, `StochasticSignalType`) наследующие `IntEnum`.
- **BREAKING**: Сигнал `compute()` возвращает Enum вместо целого числа.
- **BREAKING**: Сигнатура `get_strategy(name, config)` — config обязателен.
- Добавлена конфигурация стратегии (`DEFAULT_CONFIG`) в начало файла `macd_rsi_stoch.py`.
- `StrategyConfig` и `StrategyBuilder` сохраняются (без изменений).
- `contracts.py`, `signals.py`, `registry.py` сохраняются (с изменением сигнатуры `get_strategy`).

## Capabilities

### New Capabilities

- `indicators/signal-types`: Enum-ы типов сигналов для MACD, RSI и Stochastic. Определяют семантику каждого сигнала (бычий кроссовер, выход из зоны и т.д.) вместо сырых целых чисел.
- `indicators/directory-structure`: Подпапки для каждого индикатора (`indicators/macd/`, `indicators/rsi/`, `indicators/stochastic/`) с изоляцией кода и Enum-ов.

### Modified Capabilities

- `indicators/spec.md`: Убрать требования к дефолтным значениям и Builder-ам. Добавить требование к Enum-ам сигналов. Обновить сценарии на explicit parameters.
- `strategy-contract/spec.md`: Требование "Стратегия ДОЛЖНА принимать StrategyConfig через конструктор" — усилить: конфиг ОБЯЗАТЕЛЕН (без Optional). Обновить требование к реестру: `get_strategy(name, config)`.
- `strategy-registry/spec.md`: Сигнатура `get_strategy(name: str)` → `get_strategy(name: str, config: StrategyConfig)`.
- `strategy/builder/spec.md`: Убрать Builder-ы индикаторов из требований. Оставить `StrategyBuilder`.

## Impact

- **Изменяемые файлы**: `src/strategies/indicators/macd.py`, `src/strategies/indicators/rsi.py`, `src/strategies/indicators/stochastic.py`, `src/strategies/indicators/base.py`, `src/strategies/macd_rsi_stoch.py`, `src/strategies/registry.py`, `src/strategies/__init__.py`, `src/scheduler/runner.py`, `tools/download_snapshot_data.py`.
- **Новые файлы**: `src/strategies/indicators/macd/__init__.py`, `src/strategies/indicators/macd/indicator.py`, `src/strategies/indicators/macd/signals.py`, аналогично для `rsi/` и `stochastic/`.
- **Удаляемые файлы**: `src/strategies/indicators/macd.py`, `src/strategies/indicators/rsi.py`, `src/strategies/indicators/stochastic.py` (заменяются подпапками), Builder-ы удаляются из индикаторов.
- **Тесты**: `tests/unit/test_strategy_builder.py` — обновить (убрать тесты индикаторных Builder-ов, обновить создание индикаторов). `tests/unit/test_registry.py` — обновить вызовы `get_strategy`. `tests/unit/test_signals.py` — обновить создание `MacdRsiStochStrategy`. `tests/snapshot/test_strategies.py` — обновить.
- **Конфигурация**: `src/config.py` — не затрагивается (привязки стратегий остаются).
