# Enum-ы сигналов индикаторов

## Purpose

Определяет контракт базового Enum-а сигналов для всех технических индикаторов: общий базовый класс `BaseSignalEnum` с `NO_SIGNAL = 0` и абстрактное свойство `signal_enum` в `Indicator`.

## Requirements

### Requirement: Базовый Enum сигналов
Система ДОЛЖНА предоставлять `BaseSignalEnum(IntEnum)` в `src/strategies/indicators/base.py` с единственным членом `NO_SIGNAL = 0`. Все индикаторные Enum-ы ДОЛЖНЫ наследовать `BaseSignalEnum`.

#### Scenario: NO_SIGNAL определён один раз
- **WHEN** создаётся любой индикаторный Enum (MacdSignalEnum, RsiSignalEnum, StochasticSignalEnum)
- **THEN** он наследует `BaseSignalEnum` и содержит `NO_SIGNAL = 0` без повторного определения

#### Scenario: BaseSignalEnum доступен из base.py
- **WHEN** импортируется `from src.strategies.indicators.base import BaseSignalEnum`
- **THEN** импорт успешен, `BaseSignalEnum` является подклассом `IntEnum`

### Requirement: Абстрактное свойство signal_enum
Каждый подкласс `Indicator` ДОЛЖЕН реализовывать абстрактное свойство `signal_enum`, возвращающее класс Enum-а сигналов этого индикатора (`type[BaseSignalEnum]`).

#### Scenario: Индикатор экспортирует свой Enum
- **WHEN** создаётся `MacdIndicator(fast=12, slow=26, signal=9)`
- **THEN** `indicator.signal_enum` возвращает `MacdSignalEnum`

#### Scenario: Enum содержит все возможные сигналы
- **WHEN** вызывается `list(indicator.signal_enum)`
- **THEN** возвращается список всех членов Enum-а (включая `NO_SIGNAL`)

#### Scenario: Валидация значения через Enum
- **WHEN** вызывается `indicator.signal_enum(1)`
- **THEN** возвращается соответствующий член Enum-а; для невалидного значения выбрасывается `ValueError`

### Requirement: Именование файлов и классов
Файлы с Enum-ами сигналов ДОЛЖНЫ называться `signalEnum.py`. Классы Enum-ов ДОЛЖНЫ называться `*SignalEnum` (`MacdSignalEnum`, `RsiSignalEnum`, `StochasticSignalEnum`).

#### Scenario: Имя файла signalEnum.py
- **WHEN** создаётся Enum сигналов для MACD
- **THEN** файл расположен по пути `indicators/macd/signalEnum.py`

#### Scenario: Имя класса MacdSignalEnum
- **WHEN** определяется Enum сигналов MACD
- **THEN** класс называется `MacdSignalEnum`, наследует `BaseSignalEnum`

### Requirement: Комментарии к Enum-ам и свойствам
Члены Enum-ов ДОЛЖНЫ содержать комментарии, описывающие условие сигнала. Свойства индикаторов (`signal_column`, `signal_enum`, `warmup`) ДОЛЖНЫ содержать docstring.

#### Scenario: Комментарий к члену Enum
- **WHEN** определяется `BULLISH_CROSSOVER_BELOW_ZERO = 1` в `MacdSignalEnum`
- **THEN** строка содержит комментарий: `# Бычий кроссовер ниже нуля: macds растёт, > macd, обе < 0`

#### Scenario: Docstring к свойству signal_enum
- **WHEN** определяется свойство `signal_enum` в `MacdIndicator`
- **THEN** свойство содержит docstring: `"""Перечень возможных сигналов данного индикатора."""`
