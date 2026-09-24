## 1. Удаление кода

- [x] 1.1 Удалить `src/broker/exec_adapter.py` (`BrokerExecutionAdapter` целиком, включая импорт `MAX_RISK_PCT`)
- [x] 1.2 Удалить `BrokerExecutionPort` из `src/execution/port.py`; привести экспорт `src/execution/__init__.py` к `ExecutionPort` и `NotifyOnlyExecutionPort`
- [x] 1.3 Убедиться grep'ом, что в `src/` и `run.py` не осталось ссылок на удаляемые символы

## 2. Тесты

- [x] 2.1 Удалить `tests/unit/broker/test_entry_dedup.py`
- [x] 2.2 Сократить `tests/unit/execution/test_port.py` до покрытия `NotifyOnlyExecutionPort`
- [x] 2.3 Убедиться, что `tests/unit/test_run.py` (sqlite/addressed-брокер без legacy-адаптера) проходит без изменений

## 3. Документация и проверка

- [x] 3.1 `ARCHITECTURE.md`: убрать упоминание совместимого адаптера из таблицы модулей
- [x] 3.2 Прогнать `pytest -q` и `ruff check src tests` — зелёные