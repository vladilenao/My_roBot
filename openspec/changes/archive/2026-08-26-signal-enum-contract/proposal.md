## Why

Базовый класс `Indicator` не содержит контракт на типы сигналов индикаторов. Каждый конкретный индикатор определяет Enum сигналов самостоятельно, `NO_SIGNAL = 0` продублирован во всех трёх Enum-ах, а потребитель не может программно узнать какие сигналы поддерживает индикатор.

## What Changes

- Добавить `BaseSignalEnum(IntEnum)` с `NO_SIGNAL = 0` в `src/strategies/indicators/base.py`
- Добавить абстрактное свойство `signal_enum` в `Indicator`, возвращающее класс Enum-а сигналов
- Переименовать файлы `signals.py` → `signalEnum.py` во всех подпапках индикаторов
- Переименовать классы `*SignalType` → `*SignalEnum` (`MacdSignalType` → `MacdSignalEnum`, и т.д.)
- Сделать все индикаторные Enum-ы наследниками `BaseSignalEnum`
- Добавить комментарии к членам Enum-ов и к свойствам индикаторов (`signal_column`, `signal_enum`, `warmup`)

## Capabilities

### New Capabilities

- `indicators/signal-enum`: Контракт базового Enum-а сигналов и абстрактное свойство `signal_enum` в `Indicator`

### Modified Capabilities

_(нет — существующая spec `indicators` остаётся без изменений)_

## Impact

- `src/strategies/indicators/base.py` — добавлен `BaseSignalEnum`, абстрактное свойство `signal_enum`
- `src/strategies/indicators/macd/signals.py` → `macd/signalEnum.py` — переименование файла и класса
- `src/strategies/indicators/rsi/signals.py` → `rsi/signalEnum.py` — переименование файла и класса
- `src/strategies/indicators/stochastic/signals.py` → `stochastic/signalEnum.py` — переименование файла и класса
- `src/strategies/indicators/macd/indicator.py` — импорт `MacdSignalEnum`, реализация `signal_enum`
- `src/strategies/indicators/rsi/indicator.py` — импорт `RsiSignalEnum`, реализация `signal_enum`
- `src/strategies/indicators/stochastic/indicator.py` — импорт `StochasticSignalEnum`, реализация `signal_enum`
- `src/strategies/indicators/macd/__init__.py` — обновлён реэкспорт
- `src/strategies/indicators/rsi/__init__.py` — обновлён реэкспорт
- `src/strategies/indicators/stochastic/__init__.py` — обновлён реэкспорт
- `tests/unit/test_strategy_builder.py` — обновлены импорты и тесты Enum-ов
