## Purpose

Определяет семантические Enum-ы типов сигналов для каждого технического индикатора. Заменяет сырые целые числа (+1/-1/0) на именованные константы, обеспечивая типобезопасность и читаемость кода сигналов.

## ADDED Requirements

### Requirement: Enum типов сигналов MACD
Каждый индикатор ДОЛЖЕН предоставлять `IntEnum`-тип, описывающий типы своих сигналов. `MacdSignalType` ДОЛЖЕН содержать: `BULLISH_CROSSOVER_BELOW_ZERO` (=+1), `BEARISH_CROSSOVER_ABOVE_ZERO` (=-1), `NO_SIGNAL` (=0).

#### Scenario: Бычий кроссовер ниже нуля
- **WHEN** сигнальная линия MACD растёт, выше линии MACD, обе ниже нуля
- **THEN** `MacdSignalType.BULLISH_CROSSOVER_BELOW_ZERO` равен +1

#### Scenario: Медвежий кроссовер выше нуля
- **WHEN** сигнальная линия MACD падает, ниже линии MACD, обе выше нуля
- **THEN** `MacdSignalType.BEARISH_CROSSOVER_ABOVE_ZERO` равен -1

#### Scenario: Нет сигнала
- **WHEN** ни одно условие не выполнено
- **THEN** `MacdSignalType.NO_SIGNAL` равен 0

### Requirement: Enum типов сигналов RSI
`RsiSignalType` ДОЛЖЕН содержать: `CROSS_ABOVE_50` (=+1), `CROSS_BELOW_50` (=-1), `NO_SIGNAL` (=0).

#### Scenario: Пересечение 50 снизу вверх
- **WHEN** предыдущий RSI < 50 И текущий RSI > 50
- **THEN** `RsiSignalType.CROSS_ABOVE_50` равен +1

#### Scenario: Пересечение 50 сверху вниз
- **WHEN** предыдущий RSI > 50 И текущий RSI < 50
- **THEN** `RsiSignalType.CROSS_BELOW_50` равен -1

#### Scenario: Нет кроссовера
- **WHEN** оба значения RSI по одну сторону от 50
- **THEN** `RsiSignalType.NO_SIGNAL` равен 0

### Requirement: Enum типов сигналов Stochastic
`StochasticSignalType` ДОЛЖЕН содержать: `EXIT_OVERSOLD` (=+1), `EXIT_OVERBOUGHT` (=-1), `NO_SIGNAL` (=0).

#### Scenario: Выход из перепроданности
- **WHEN** предыдущая K < 20 И текущая K > 20 и < 50 И K растёт
- **THEN** `StochasticSignalType.EXIT_OVERSOLD` равен +1

#### Scenario: Выход из перекупленности
- **WHEN** предыдущая K > 80 И текущая K < 80 и > 50 И K падает
- **THEN** `StochasticSignalType.EXIT_OVERBOUGHT` равен -1

#### Scenario: Нет сигнала
- **WHEN** ни одно условие не выполнено
- **THEN** `StochasticSignalType.NO_SIGNAL` равен 0

### Requirement: IntEnum для совместимости с арифметикой
Все Enum-ы типов сигналов ДОЛЖНЫ наследоваться от `IntEnum`, чтобы обеспечить совместимость с арифметическими операциями (суммирование) в методе `decide()` стратегии.

#### Scenario: Суммирование Enum-ов
- **WHEN** выполняется `sum([MacdSignalType.BULLISH_CROSSOVER_BELOW_ZERO, MacdSignalType.BULLISH_CROSSOVER_BELOW_ZERO])`
- **THEN** результат равен 2

#### Scenario: Сравнение с нулём
- **WHEN** выполняется `MacdSignalType.NO_SIGNAL == 0`
- **THEN** результат `True`

### Requirement: Хранение Enum-ов в подпапке индикатора
Каждый Enum ДОЛЖЕН храниться в отдельном файле `signals.py` внутри подпапки соответствующего индикатора (`indicators/macd/signals.py`, `indicators/rsi/signals.py`, `indicators/stochastic/signals.py`).

#### Scenario: Импорт Enum-а MACD
- **WHEN** выполняется `from src.strategies.indicators.macd.signals import MacdSignalType`
- **THEN** импорт успешен, Enum доступен
