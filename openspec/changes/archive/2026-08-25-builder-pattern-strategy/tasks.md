## 1. Индикаторы (indicators/)

- [x] 1.1 Создать `src/strategies/indicators/base.py` — Indicator ABC с abstract methods `warmup`, `compute`, property `signal_column`
- [x] 1.2 Создать `src/strategies/indicators/macd.py` — MacdIndicator (frozen dataclass) + MacdIndicatorBuilder + compute()
- [x] 1.3 Создать `src/strategies/indicators/rsi.py` — RsiIndicator (frozen dataclass) + RsiIndicatorBuilder + compute()
- [x] 1.4 Создать `src/strategies/indicators/stochastic.py` — StochasticIndicator (frozen dataclass) + StochasticIndicatorBuilder + compute()
- [x] 1.5 Создать `src/strategies/indicators/__init__.py` — экспорт всех типов

## 2. Стратегия и signals

- [x] 2.1 Создать `src/strategies/signals.py` — перенести `get_last_signals()` из `macd_rsi_stoch/signals/aggregate.py`
- [x] 2.2 Создать `src/strategies/strategy.py` — StrategyConfig (frozen dataclass) + StrategyBuilder

## 3. MacdRsiStochStrategy

- [x] 3.1 Создать `src/strategies/macd_rsi_stoch.py` — новый класс, принимающий StrategyConfig
- [x] 3.2 Обновить `src/strategies/__init__.py` — обновить реестр и импорты

## 4. Удаление старой структуры

- [x] 4.1 Удалить `src/strategies/macd_rsi_stoch/` (весь пакет)

## 5. Тесты

- [x] 5.1 Создать `tests/unit/test_strategy_builder.py` — unit-тесты Builder'а, валидации, frozen-поведения
- [x] 5.2 Обновить `tests/unit/test_signals.py` — обновить импорты
- [x] 5.3 Обновить `tests/unit/test_registry.py` — обновить импорты (без изменений)

## 6. Верификация

- [x] 6.1 Запустить `pytest` — все тесты проходят (200 passed)
