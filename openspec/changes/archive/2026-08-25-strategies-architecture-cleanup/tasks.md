## 1. Создание registry.py

- [x] 1.1 Создать `src/strategies/registry.py` с функцией `register`, `_registry`, `_discover_strategies`, `get_strategy`, `all_strategies`, `strategy_names`, `validate_assignments`
- [x] 1.2 Добавить type hints для всех функций реестра
- [x] 1.3 Реализовать auto-discovery через `pkgutil.iter_modules`

## 2. Переименование base.py → contracts.py

- [x] 2.1 Переименовать `src/strategies/base.py` в `src/strategies/contracts.py`
- [x] 2.2 Обновить импорты в `src/strategies/macd_rsi_stoch.py`
- [x] 2.3 Обновить импорты в `src/strategies/indicators/*.py`
- [x] 2.4 Обновить импорты в `tests/unit/test_registry.py`
- [x] 2.5 Обновить импорты в `tests/unit/test_strategy_builder.py`

## 3. Обновление __init__.py

- [x] 3.1 Переписать `src/strategies/__init__.py`: только `__all__` и re-exports
- [x] 3.2 Убрать lazy loading и валидацию из `__init__.py`

## 4. Удаление старого base.py

- [x] 4.1 Удалить `src/strategies/base.py`

## 5. Тестирование

- [x] 5.1 Запустить unit-тесты: `pytest tests/unit/ -v`
- [x] 5.2 Запустить snapshot-тесты: `pytest tests/snapshot/ -v`
- [x] 5.3 Проверить, что все 202 теста проходят
