# Strategy Builder

## Purpose

Предоставляет иммутабельную конфигурацию стратегии (`StrategyConfig`) и Builder для её сборки из списка индикаторов. Конфигурация заменяет хардкод параметров стратегии и становится единственным источником параметров для `MacdRsiStochStrategy`.

## Requirements

### Requirement: StrategyConfig — frozen dataclass
`StrategyConfig` ДОЛЖЕН быть `dataclass(frozen=True)` с полями: `name` (str), `strategy_window` (int), `indicators` (tuple of Indicator).

#### Scenario: Конфиг неизменяем
- **WHEN** создан `StrategyConfig`
- **THEN** попытка изменения атрибута вызывает `FrozenInstanceError`

#### Scenario: Indicators — tuple для иммутабельности
- **WHEN** создаётся `StrategyConfig` со списком индикаторов
- **THEN** `indicators` хранится как `tuple`, не `list`

### Requirement: Required history — вычисляемое свойство
`StrategyConfig` ДОЛЖЕН предоставлять property `required_history`, вычисляемый как `strategy_window + max(i.warmup for i in indicators)`.

#### Scenario: Required history учитывает самый «медленный» индикатор
- **WHEN** конфиг содержит `MacdIndicator()` (warmup=35) и `RsiIndicator()` (warmup=14) с `strategy_window=5`
- **THEN** `required_history == 40`

#### Scenario: Required history без индикаторов
- **WHEN** конфиг содержит пустой список индикаторов
- **THEN** `required_history == strategy_window`

### Requirement: Signal columns — вычисляемое свойство
`StrategyConfig` ДОЛЖЕН предоставлять property `signal_columns`, возвращающий список имён сигнальных столбцов из всех индикаторов.

#### Scenario: Signal columns из индикаторов
- **WHEN** конфиг содержит `MacdIndicator()`, `RsiIndicator()`, `StochasticIndicator()`
- **THEN** `signal_columns == ["macd_signal", "rsi_signal", "stoch_signal"]`

### Requirement: StrategyBuilder — сборка конфигурации
`StrategyBuilder` ДОЛЖЕН предоставлять методы: `set_name(name)`, `set_strategy_window(window)`, `add_indicator(indicator)`, `build()`. Все setter-ы возвращают `self` для chaining.

#### Scenario: Полная сборка
- **WHEN** вызывается `StrategyBuilder().set_name("test").set_strategy_window(5).add_indicator(MacdIndicator()).build()`
- **THEN** возвращается `StrategyConfig` с указанными параметрами

#### Scenario: Имя обязательно
- **WHEN** вызывается `StrategyBuilder().build()` без `set_name()`
- **THEN** выбрасывается `ValueError` с сообщением об обязательном имени

#### Scenario: Минимум один индикатор
- **WHEN** вызывается `StrategyBuilder().set_name("test").build()` без индикаторов
- **THEN** выбрасывается `ValueError` с сообщением о необходимости хотя бы одного индикатора

#### Scenario: Window положительный
- **WHEN** вызывается `StrategyBuilder().set_name("test").set_strategy_window(0).build()`
- **THEN** выбрасывается `ValueError` с сообщением о положительном окне
