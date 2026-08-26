## 1. Базовый Enum и контракт Indicator

- [x] 1.1 Добавить `BaseSignalEnum(IntEnum)` с `NO_SIGNAL = 0` в `src/strategies/indicators/base.py`
- [x] 1.2 Добавить абстрактное свойство `signal_enum` в `Indicator`: `@property @abstractmethod def signal_enum(self) -> type[BaseSignalEnum]`

## 2. Переименование Enum-ов MACD

- [x] 2.1 Переименовать файл `indicators/macd/signals.py` → `indicators/macd/signalEnum.py`
- [x] 2.2 Переименовать класс `MacdSignalType` → `MacdSignalEnum`, наследовать `BaseSignalEnum`
- [x] 2.3 Добавить комментарии к членам `BULLISH_CROSSOVER_BELOW_ZERO` и `BEARISH_CROSSOVER_ABOVE_ZERO`
- [x] 2.4 Обновить `indicators/macd/__init__.py`: реэкспорт `MacdSignalEnum`

## 3. Переименование Enum-ов RSI

- [x] 3.1 Переименовать файл `indicators/rsi/signals.py` → `indicators/rsi/signalEnum.py`
- [x] 3.2 Переименовать класс `RsiSignalType` → `RsiSignalEnum`, наследовать `BaseSignalEnum`
- [x] 3.3 Добавить комментарии к членам `CROSS_ABOVE_50` и `CROSS_BELOW_50`
- [x] 3.4 Обновить `indicators/rsi/__init__.py`: реэкспорт `RsiSignalEnum`

## 4. Переименование Enum-ов Stochastic

- [x] 4.1 Переименовать файл `indicators/stochastic/signals.py` → `indicators/stochastic/signalEnum.py`
- [x] 4.2 Переименовать класс `StochasticSignalType` → `StochasticSignalEnum`, наследовать `BaseSignalEnum`
- [x] 4.3 Добавить комментарии к членам `EXIT_OVERSOLD` и `EXIT_OVERBOUGHT`
- [x] 4.4 Обновить `indicators/stochastic/__init__.py`: реэкспорт `StochasticSignalEnum`

## 5. Реализация signal_enum в индикаторах

- [x] 5.1 В `macd/indicator.py`: импорт `MacdSignalEnum`, реализовать `signal_enum` → `MacdSignalEnum`, добавить docstring к `signal_column`, `signal_enum`, `warmup`
- [x] 5.2 В `rsi/indicator.py`: импорт `RsiSignalEnum`, реализовать `signal_enum` → `RsiSignalEnum`, добавить docstring к `signal_column`, `signal_enum`, `warmup`
- [x] 5.3 В `stochastic/indicator.py`: импорт `StochasticSignalEnum`, реализовать `signal_enum` → `StochasticSignalEnum`, добавить docstring к `signal_column`, `signal_enum`, `warmup`

## 6. Обновление импортов

- [x] 6.1 Обновить импорты в `src/strategies/macd_rsi_stoch.py` (если используются Enum-ы)
- [x] 6.2 Обновить импорты в `src/strategies/indicators/__init__.py` (если реэкспортирует Enum-ы)
- [x] 6.3 Обновить импорты в `tests/unit/test_strategy_builder.py`: `MacdSignalType` → `MacdSignalEnum`, `RsiSignalType` → `RsiSignalEnum`, `StochasticSignalType` → `StochasticSignalEnum`
- [x] 6.4 Обновить импорты в `tests/unit/test_signals.py` (если используются Enum-ы)

## 7. Тесты

- [x] 7.1 Добавить тест: `BaseSignalEnum.NO_SIGNAL == 0`
- [x] 7.2 Добавить тест: каждый индикатор возвращает свой Enum через `signal_enum`
- [x] 7.3 Добавить тест: `list(indicator.signal_enum)` содержит все члены Enum-а
- [x] 7.4 Добавить тест: `indicator.signal_enum(1)` возвращает корректный член
- [x] 7.5 Убедиться что существующие тесты проходят после переименования

## 8. Валидация

- [x] 8.1 Запустить `ruff check src/ tests/` — исправить линтер ошибки
- [x] 8.2 Запустить `pytest tests/unit/ -v` — все unit-тесты проходят
- [x] 8.3 Запустить `pytest tests/snapshot/ -v` — snapshot-тесты проходят
