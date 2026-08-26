# Технические индикаторы

## Purpose

Вычисляет индикаторы технического анализа (MACD, RSI, Стохастик) на основе OHLCV-данных и генерирует столбцы сигналов для каждой свечи (+1 бычий, -1 медвежий, 0 нейтральный).

## Requirements

### Requirement: Основная функция анализа
- Система ДОЛЖНА предоставлять функцию `tech_analyze(data)`, принимающую OHLCV DataFrame и возвращающую дополненную копию.
- Исходный DataFrame НЕ ДОЛЖЕН быть изменён.
- Альтернативно: каждый индикатор ДОЛЖЕН предоставлять метод `compute(df)`, вычисляющий индикатор и генерирующий сигнальный столбец на копии DataFrame.
- Индикаторы НЕ ДОЛЖНЫ иметь дефолтные значения параметров — все параметры обязательны при создании.

#### Scenario: Входной DataFrame не изменяется
- **WHEN** вызывается `tech_analyze(data)` или `indicator.compute(data)`
- **THEN** исходный `data` сохраняет оригинальные столбцы

#### Scenario: Возвращается дополненная копия
- **WHEN** `tech_analyze` или `indicator.compute` вызывается с OHLCV DataFrame
- **THEN** возвращается новый DataFrame с теми же строками и дополнительными столбцами

#### Scenario: Все параметры обязательны
- **WHEN** создаётся `MacdIndicator()` без параметров
- **THEN** выбрасывается `TypeError` (отсутствуют обязательные аргументы fast, slow, signal)

### Requirement: Вычисление MACD
- MACD вычисляется с параметрами, переданными при создании `MacdIndicator(fast, slow, signal)`, по цене `close`.
- Параметры ОБЯЗАТЕЛЬНЫ — дефолтные значения отсутствуют.

#### Scenario: MACD присутствует
- **WHEN** `tech_analyze` или `MacdIndicator(fast=12, slow=26, signal=9).compute()` вызывается с OHLCV DataFrame
- **THEN** в результате есть столбцы MACD и сигнальный столбец `macd_signal`

#### Scenario: Конфигурируемые параметры
- **WHEN** создаётся `MacdIndicator(fast=8, slow=17, signal=5)` и вызывается `.compute(data)`
- **THEN** MACD вычисляется с указанными параметрами

### Requirement: Вычисление Стохастик-осциллятора
- Стохастик вычисляется с параметрами, переданными при создании `StochasticIndicator(k, d, smooth_k)`, по цене `close`.
- Параметры ОБЯЗАТЕЛЬНЫ — дефолтные значения отсутствуют.

#### Scenario: Стохастик присутствует
- **WHEN** `tech_analyze` или `StochasticIndicator(k=14, d=3, smooth_k=3).compute()` вызывается
- **THEN** в результате есть столбцы Stochastic и сигнальный столбец `stoch_signal`

#### Scenario: Конфигурируемые параметры
- **WHEN** создаётся `StochasticIndicator(k=9, d=3, smooth_k=3)` и вызывается `.compute(data)`
- **THEN** Stochastic вычисляется с указанными параметрами

### Requirement: Вычисление RSI
- RSI вычисляется с периодом, переданным при создании `RsiIndicator(period)`.
- Параметр ОБЯЗАТЕЛЕН — дефолтное значение отсутствует.

#### Scenario: RSI присутствует
- **WHEN** `tech_analyze` или `RsiIndicator(period=14).compute()` вызывается
- **THEN** в результате есть столбец RSI и сигнальный столбец `rsi_signal`

#### Scenario: Конфигурируемый период
- **WHEN** создаётся `RsiIndicator(period=7)` и вызывается `.compute(data)`
- **THEN** RSI вычисляется с периодом 7

### Requirement: Нормализация имён столбцов
- Все имена столбцов результата ДОЛЖНЫ быть в нижнем регистре.

#### Scenario: Имена в нижнем регистре
- **WHEN** `tech_analyze` возвращает результат
- **THEN** все имена столбцов в нижнем регистре

### Requirement: Генерация MACD-сигнала
- `macd_signal` возвращает `MacdSignalType`: `BULLISH_CROSSOVER_BELOW_ZERO` (+1) — бычий кроссовер ниже нуля; `BEARISH_CROSSOVER_ABOVE_ZERO` (-1) — медвежий кроссовер выше нуля; `NO_SIGNAL` (0) — иначе.

#### Scenario: Бычий сигнал MACD
- **WHEN** `macds` растёт И `macds > macd` И `macd < 0` И `macds < 0`
- **THEN** `macd_signal = MacdSignalType.BULLISH_CROSSOVER_BELOW_ZERO`

#### Scenario: Медвежий сигнал MACD
- **WHEN** `macds` падает И `macds < macd` И `macd > 0` И `macds > 0`
- **THEN** `macd_signal = MacdSignalType.BEARISH_CROSSOVER_ABOVE_ZERO`

### Requirement: Генерация RSI-сигнала
- `rsi_signal` возвращает `RsiSignalType`: `CROSS_ABOVE_50` (+1) — RSI пересекает 50 снизу вверх; `CROSS_BELOW_50` (-1) — сверху вниз; `NO_SIGNAL` (0) — иначе.

#### Scenario: Бычий кроссовер RSI
- **WHEN** предыдущий RSI < 50 И текущий RSI > 50
- **THEN** `rsi_signal = RsiSignalType.CROSS_ABOVE_50`

#### Scenario: Медвежий кроссовер RSI
- **WHEN** предыдущий RSI > 50 И текущий RSI < 50
- **THEN** `rsi_signal = RsiSignalType.CROSS_BELOW_50`

#### Scenario: Нет кроссовера
- **WHEN** оба значения RSI по одну сторону от 50
- **THEN** `rsi_signal = RsiSignalType.NO_SIGNAL`

### Requirement: Генерация Стохастик-сигнала
- `stoch_signal` возвращает `StochasticSignalType`: `EXIT_OVERSOLD` (+1) — выход из перепроданности; `EXIT_OVERBOUGHT` (-1) — выход из перекупленности; `NO_SIGNAL` (0) — иначе.

#### Scenario: Бычий кроссовер из перепроданности
- **WHEN** предыдущая K < 20 И текущая K > 20 и < 50 И K растёт
- **THEN** `stoch_signal = StochasticSignalType.EXIT_OVERSOLD`

#### Scenario: Медвежий кроссовер из перекупленности
- **WHEN** предыдущая K > 80 И текущая K < 80 и > 50 И K падает
- **THEN** `stoch_signal = StochasticSignalType.EXIT_OVERBOUGHT`
