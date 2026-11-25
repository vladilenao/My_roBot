## 1. Системный поиск адреса

- [x] 1.1 `src/grpc_fork.py`: `GRPC_DNS_RESOLVER=native` по умолчанию рядом с `GRPC_ENABLE_FORK_SUPPORT`; значение снаружи сохраняется.

## 2. Тесты

- [x] 2.1 `tests/unit/assembly/test_contour_process.py`: импорт модуля в чистом окружении даёт `native`, заданное снаружи значение не меняется; процесс контура, запущенный `ContourProcess`, видит то же значение.
- [x] 2.2 Полный `pytest` и `ruff check src tests run.py`.
  Сделано 25.11.2026: полный `pytest` в хуке коммита; на прежнем коде `test_system_resolver_by_default` не проходит. Проверка у брокера с поддельным токеном после `import src.grpc_fork` без `GRPC_DNS_RESOLVER` в окружении: `UNAUTHENTICATED` (адрес найден), без модуля — `UNAVAILABLE`.

## 3. Живая проверка на тестовом роботе

- [ ] 3.1 Перезапуск тестового робота из PyCharm без `GRPC_DNS_RESOLVER` в настройке запуска, при неотвечающем 8.8.8.8: контуры «Счёт» и песочница стартуют, в журналах ядра и контуров нет `Timeout while contacting DNS servers`.
