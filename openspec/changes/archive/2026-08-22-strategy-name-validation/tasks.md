# Задачи: strategy-name-validation

## 1. Статический перечень

- [x] 1.1 Создать `src/strategies/names.py`: `StrategyName = Literal["macd_rsi_stoch"]`; модуль импортирует только `typing`
- [x] 1.2 Аннотировать в `src/config.py`: `STRATEGY_ASSIGNMENTS: dict[str, list[StrategyName]]` с импортом из `src.strategies.names`

## 2. Реестр и движок

- [x] 2.1 Ленивая регистрация в `src/strategies/__init__.py`: убрать module-level импорт macd_rsi_stoch; добавить `_ensure_registered()` (однократный импорт пакетов стратегий под флагом `_packages_loaded`, идемпотентность по флагу, а не по пустоте словаря) и вызвать её в начале `get_strategy` и `all_strategies`
- [x] 2.2 Добавить в `src/strategies/__init__.py`: `strategy_names()` (отсортированные ключи реестра) и `validate_assignments(assignments)` (ValueError со списками неизвестных и доступных имён); обе начинаются с `_ensure_registered()`
- [x] 2.3 Вызвать `validate_assignments(STRATEGY_ASSIGNMENTS)` в начале `run_bot()` до цикла

## 3. Тесты и верификация

- [x] 3.1 `tests/unit/test_registry.py`: тест консистентности Literal↔реестр (равенство множеств через get_args; учесть autouse-фикстуру clean_registry — зарегистрировать MacdRsiStochStrategy вручную в тесте), тесты ленивой регистрации (первый доступ загружает пакеты, повторный — нет), тесты `strategy_names` и `validate_assignments` (валидный словарь проходит, неизвестное имя — ValueError с обоими списками)
- [x] 3.2 Сценарий «Лёгкость импорта»: subprocess-тест с чистым интерпретатором — после `import src.strategies.names` в sys.modules отсутствуют pandas_ta и подмодули `src.strategies.macd_rsi_stoch`
- [x] 3.3 `tests/unit/test_runner.py`: fail-fast — при невалидном ASSIGNMENTS `run_bot` падает до обращения к load_candles; обновить существующие заглушки ASSIGNMENTS под валидацию
- [x] 3.4 Полный `pytest` зелёный
