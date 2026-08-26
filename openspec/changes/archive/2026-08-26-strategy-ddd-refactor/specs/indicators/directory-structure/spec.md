## Purpose

Определяет иерархическую структуру каталогов для технических индикаторов. Каждый индикатор размещается в собственной подпапке с изолированным кодом расчёта и Enum-ом типов сигналов.

## ADDED Requirements

### Requirement: Подпапки для каждого индикатора
Каждый технический индикатор ДОЛЖЕН располагаться в собственной подпапке внутри `src/strategies/indicators/`: `macd/`, `rsi/`, `stochastic/`. Подпапка ДОЛЖНА содержать: `__init__.py` (реэкспорт публичных символов), `indicator.py` (класс индикатора), `signals.py` (Enum типов сигналов).

#### Scenario: Структура каталога MACD
- **WHEN** анализируется содержимое `src/strategies/indicators/macd/`
- **THEN** присутствуют файлы `__init__.py`, `indicator.py`, `signals.py`

#### Scenario: Структура каталога RSI
- **WHEN** анализируется содержимое `src/strategies/indicators/rsi/`
- **THEN** присутствуют файлы `__init__.py`, `indicator.py`, `signals.py`

#### Scenario: Структура каталога Stochastic
- **WHEN** анализируется содержимое `src/strategies/indicators/stochastic/`
- **THEN** присутствуют файлы `__init__.py`, `indicator.py`, `signals.py`

### Requirement: __init__.py реэкспортирует публичные символы
Каждый `__init__.py` в подпапке индикатора ДОЛЖЕН реэкспортировать класс индикатора и Enum сигналов для удобства импорта: `from .indicator import MacdIndicator` и `from .signals import MacdSignalType`.

#### Scenario: Импорт через __init__.py
- **WHEN** выполняется `from src.strategies.indicators.macd import MacdIndicator, MacdSignalType`
- **THEN** оба символа доступны без указания подмодулей

### Requirement: Базовый класс в indicators/base.py
Абстрактный базовый класс `Indicator` ДОЛЖЕН оставаться в `src/strategies/indicators/base.py` (не в подпапке). Подпапки индикаторов импортируют его через `from src.strategies.indicators.base import Indicator`.

#### Scenario: Импорт базового класса из подпапки
- **WHEN** `indicator.py` внутри `indicators/macd/` импортирует `Indicator`
- **THEN** импорт выполняется из `src.strategies.indicators.base`

### Requirement: Плоские файлы заменяются подпапками
Существующие плоские файлы `indicators/macd.py`, `indicators/rsi.py`, `indicators/stochastic.py` ДОЛЖНЫ быть удалены и заменены соответствующими подпапками.

#### Scenario: Плоские файлы отсутствуют
- **WHEN** анализируется содержимое `src/strategies/indicators/`
- **THEN** файлы `macd.py`, `rsi.py`, `stochastic.py` отсутствуют
