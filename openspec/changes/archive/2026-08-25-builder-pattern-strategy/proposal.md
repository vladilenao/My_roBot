## Why

Параметры индикаторов (MACD, RSI, Stochastic) и окно агрегации захардкожены внутри `MacdRsiStochStrategy` и `calculator.py`. Это делает невозможным бэктестинг разных конфигураций без правки кода стратегии. Для каждой новой комбинации параметров приходится создавать новый класс или дублировать логику.

## What Changes

- **NEW**: Модуль `src/strategies/indicators/` — иерархия `Indicator` (frozen dataclass) с подклассами `MacdIndicator`, `RsiIndicator`, `StochasticIndicator`, каждый со своим `IndicatorBuilder` и методом `compute()`.
- **NEW**: `src/strategies/strategy.py` — `StrategyConfig` (frozen dataclass) и `StrategyBuilder` для сборки конфигурации стратегии из списка индикаторов.
- **BREAKING**: `MacdRsiStochStrategy` рефакторится — принимает `StrategyConfig` через конструктор вместо хардкода параметров.
- **BREAKING**: `calculator.py` (`tech_analyze`) заменяется на полиморфный вызов `indicator.compute(df)` для каждого индикатора из конфига.
- **BREAKING**: Структура `macd_rsi_stoch/` (подпапки `indicators/`, `signals/`) заменяется одиночным файлом `macd_rsi_stoch.py` + общими модулями `signals.py` и `indicators/`.
- **MODIFIED**: Реестр стратегий адаптируется под новую схему — `get_strategy()` работает с `StrategyConfig`.

## Capabilities

### New Capabilities
- `indicators/builder`: Компонентный Builder Pattern для индикаторов — каждый индикатор (MACD, RSI, Stochastic) имеет собственный frozen dataclass, Builder с method chaining и метод `compute()`.
- `strategy/builder`: StrategyConfig + StrategyBuilder — иммутабельная конфигурация стратегии, собирающая список индикаторов через Builder.

### Modified Capabilities
- `indicators/spec.md`: Параметры индикаторов становятся конфигурируемыми (были хардкод). Каждый индикатор вычисляется собственным методом `compute()`, а не единой функцией `tech_analyze`.
- `strategy-contract/spec.md`: Стратегия принимает `StrategyConfig` через конструктор. Структура пакета стратегии упрощается (один файл вместо подпапок). Реестр адаптируется.
- `signals/spec.md`: Сигнальные колонки определяются динамически из `StrategyConfig.indicators`, а не хардкодятся.

## Impact

- `src/strategies/macd_rsi_stoch/` — удаляется (все файлы)
- `src/strategies/macd_rsi_stoch.py` — создаётся (один файл)
- `src/strategies/indicators/` — создаётся (5 файлов)
- `src/strategies/strategy.py` — создаётся
- `src/strategies/signals.py` — создаётся
- `src/strategies/__init__.py` — обновляется
- `tests/unit/test_strategy_builder.py` — создаётся
- `tests/unit/test_signals.py` — обновляется (импорты)
- `tests/unit/test_registry.py` — обновляется (импорты)
- `tests/snapshot/test_strategies.py` — без изменений (контракт сохраняется)
