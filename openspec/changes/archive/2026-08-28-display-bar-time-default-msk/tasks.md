## 1. Конфигурация

- [x] 1.1 Изменить `BAR_TIME_TZ_OFFSET_HOURS` в `src/config.py` с `0` на `3` (МСК).

## 2. Проверка

- [x] 2.1 Убедиться, что фабрика `get_notifier()` создаёт форматтер со смещением 3 (smoke-проверка с `bar_time=07:06 UTC` → вывод `10:06`).
- [x] 2.2 Прогнать `pytest tests/unit` — все модульные тесты проходят.
- [x] 2.3 Прогнать `ruff check` по `src/config.py`, `src/notifier/base.py`, `src/notifier/__init__.py`.
