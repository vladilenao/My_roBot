## MODIFIED Requirements

### Requirement: Основная функция анализа
- Система ДОЛЖНА предоставлять функцию `tech_analyze(data)`, принимающую OHLCV DataFrame и возвращающую дополненную копию.
- Исходный DataFrame НЕ ДОЛЖЕН быть изменён.
- Альтернативно: каждый индикатор ДОЛЖЕН предоставлять метод `compute(df)`, вычисляющий индикатор и генерирующий сигнальный столбец на копии DataFrame.

#### Scenario: Входной DataFrame не изменяется
- **WHEN** вызывается `tech_analyze(data)` или `indicator.compute(data)`
- **THEN** исходный `data` сохраняет оригинальные столбцы

#### Scenario: Возвращается дополненная копия
- **WHEN** `tech_analyze` или `indicator.compute` вызывается с OHLCV DataFrame
- **THEN** возвращается новый DataFrame с теми же строками и дополнительными столбцами

### Requirement: Вычисление MACD
- MACD вычисляется с параметрами по умолчанию: fast=12, slow=26, signal=9, по цене `close`.
- Параметры ДОЛЖНЫ быть конфигурируемыми через `MacdIndicator` и `MacdIndicatorBuilder`.

#### Scenario: MACD присутствует
- **WHEN** `tech_analyze` или `MacdIndicator().compute()` вызывается с OHLCV DataFrame
- **THEN** в результате есть столбцы MACD и сигнальный столбец `macd_signal`

#### Scenario: Конфигурируемые параметры
- **WHEN** создаётся `MacdIndicator(fast=8, slow=17, signal=5)` и вызывается `.compute(data)`
- **THEN** MACD вычисляется с указанными параметрами

### Requirement: Вычисление Стохастик-осциллятора
- Стохастик вычисляется с параметрами по умолчанию: K=14, D=3, smooth_k=3, по цене `close`.
- Параметры ДОЛЖНЫ быть конфигурируемыми через `StochasticIndicator` и `StochasticIndicatorBuilder`.

#### Scenario: Стохастик присутствует
- **WHEN** `tech_analyze` или `StochasticIndicator().compute()` вызывается
- **THEN** в результате есть столбцы Stochastic и сигнальный столбец `stoch_signal`

#### Scenario: Конфигурируемые параметры
- **WHEN** создаётся `StochasticIndicator(k=9, d=3, smooth_k=3)` и вызывается `.compute(data)`
- **THEN** Stochastic вычисляется с указанными параметрами

### Requirement: Вычисление RSI
- RSI вычисляется с периодом 14 по умолчанию.
- Параметр ДОЛЖЕН быть конфигурируемым через `RsiIndicator` и `RsiIndicatorBuilder`.

#### Scenario: RSI присутствует
- **WHEN** `tech_analyze` или `RsiIndicator().compute()` вызывается
- **THEN** в результате есть столбец RSI и сигнальный столбец `rsi_signal`

#### Scenario: Конфигурируемый период
- **WHEN** создаётся `RsiIndicator(period=7)` и вызывается `.compute(data)`
- **THEN** RSI вычисляется с периодом 7

## REMOVED Requirements

### Requirement: Единая функция tech_analyze
**Reason**: Заменяется на полиморфный вызов `indicator.compute(df)` для каждого индикатора из конфига. Функция `tech_analyze` сохраняется для обратной совместимости, но параметры индикаторов больше не хардкодятся внутри неё.
**Migration**: Используйте `indicator.compute(df)` для каждого индикатора из `StrategyConfig.indicators`.
