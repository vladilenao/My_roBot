## 1. Создание структуры каталогов индикаторов

- [x] 1.1 Создать подпапку `src/strategies/indicators/macd/` с файлами `__init__.py`, `indicator.py`, `signals.py`
- [x] 1.2 Создать подпапку `src/strategies/indicators/rsi/` с файлами `__init__.py`, `indicator.py`, `signals.py`
- [x] 1.3 Создать подпапку `src/strategies/indicators/stochastic/` с файлами `__init__.py`, `indicator.py`, `signals.py`

## 2. Реализация Enum-ов типов сигналов

- [x] 2.1 Создать `MacdSignalType(IntEnum)` в `indicators/macd/signals.py`: `BULLISH_CROSSOVER_BELOW_ZERO=1`, `BEARISH_CROSSOVER_ABOVE_ZERO=-1`, `NO_SIGNAL=0`
- [x] 2.2 Создать `RsiSignalType(IntEnum)` в `indicators/rsi/signals.py`: `CROSS_ABOVE_50=1`, `CROSS_BELOW_50=-1`, `NO_SIGNAL=0`
- [x] 2.3 Создать `StochasticSignalType(IntEnum)` в `indicators/stochastic/signals.py`: `EXIT_OVERSOLD=1`, `EXIT_OVERBOUGHT=-1`, `NO_SIGNAL=0`

## 3. Перенос индикаторов в подпапки (без дефолтов, с Enum-ами)

- [x] 3.1 Перенести `MacdIndicator` в `indicators/macd/indicator.py`: убрать дефолты (`fast`, `slow`, `signal` обязательны), заменить `np.where` на `MacdSignalType`, обновить `__init__.py` для реэкспорта
- [x] 3.2 Перенести `RsiIndicator` в `indicators/rsi/indicator.py`: убрать дефолт `period`, заменить `np.where` на `RsiSignalType`, обновить `__init__.py`
- [x] 3.3 Перенести `StochasticIndicator` в `indicators/stochastic/indicator.py`: убрать дефолты (`k`, `d`, `smooth_k` обязательны), заменить `np.where` на `StochasticSignalType`, обновить `__init__.py`

## 4. Удаление старых файлов и Builder-ов

- [x] 4.1 Удалить плоские файлы `indicators/macd.py`, `indicators/rsi.py`, `indicators/stochastic.py`
- [x] 4.2 Удалить классы `MacdIndicatorBuilder`, `RsiIndicatorBuilder`, `StochasticIndicatorBuilder` (были в удалённых файлах)

## 5. Обновление стратегии

- [x] 5.1 В `macd_rsi_stoch.py`: добавить `DEFAULT_CONFIG = StrategyConfig(...)` в начало файла с явными параметрами (MACD 12/26/9, RSI 14, Stoch 14/3/3, window=5)
- [x] 5.2 Удалить функцию `_build_default_config()` из `macd_rsi_stoch.py`
- [x] 5.3 Сделать `config` обязательным в `MacdRsiStochStrategy.__init__`: убрать `Optional` и fallback

## 6. Обновление реестра

- [x] 6.1 Изменить сигнатуру `get_strategy(name: str, config: StrategyConfig) -> Strategy` в `registry.py`
- [x] 6.2 Передавать `config` в конструктор: `_registry[name](config=config)`
- [x] 6.3 Обновить `all_stratures()` — оставить без config (используется только в тестах, дефолты допустимы)

## 7. Обновление вызывающего кода

- [x] 7.1 В `runner.py`: добавить импорты индикаторов и `StrategyConfig`, собирать `config` явно с параметрами MACD 12/26/9, RSI 14, Stoch 14/3/3, window=5, передавать в `get_strategy(strategy_name, config=config)`
- [x] 7.2 В `tools/download_snapshot_data.py`: аналогично собирать config и передавать в `get_strategy()`

## 8. Обновление тестов

- [x] 8.1 `tests/unit/test_strategy_builder.py`: обновить создание индикаторов (без дефолтов), удалить тесты индикаторных Builder-ов
- [x] 8.2 `tests/unit/test_registry.py`: обновить вызовы `get_strategy()` — передавать config
- [x] 8.3 `tests/unit/test_signals.py`: обновить создание `MacdRsiStochStrategy()` — передавать config
- [x] 8.4 `tests/snapshot/test_strategies.py`: обновить создание стратегии — передавать config

## 9. Валидация

- [x] 9.1 Запустить `ruff check src/ tests/` — исправить линтер ошибки
- [x] 9.2 Запустить `pytest tests/unit/ -v` — все unit-тесты проходят
- [x] 9.3 Запустить `pytest tests/snapshot/ -v` — snapshot-тесты проходят
