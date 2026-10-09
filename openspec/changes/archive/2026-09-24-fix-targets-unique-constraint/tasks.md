## 1. Миграция схемы V6 → V7

- [x] 1.1 В `src/trade_journal/schema.py` изменить `CREATE TABLE targets` в `SCHEMA_SQL`: заменить `target_id TEXT PRIMARY KEY` на составной `PRIMARY KEY (trade_id, target_id)`, сохранив порядок колонок и `UNIQUE (trade_id, target_index)`.
- [x] 1.2 Поднять `SCHEMA_VERSION = 7`.
- [x] 1.3 Добавить `MIGRATE_V6_TO_V7_SQL`: пересоздание таблицы `targets` с тем же порядком колонок и переносом данных (`CREATE TABLE targets_new …; INSERT INTO targets_new SELECT … FROM targets; DROP TABLE targets; ALTER TABLE targets_new RENAME TO targets;`).
- [x] 1.4 В `initialize_schema` добавить ветку `version == 6` (только `MIGRATE_V6_TO_V7_SQL`) и продлить цепочки версий 1–5 до v7.

## 2. Устойчивость вставки целей

- [x] 2.1 В `src/trade_management/manager.py` перевести `INSERT INTO targets VALUES (?, …)` на явный список колонок (защита от зависимости от порядка колонок).

## 3. Русское сообщение об ошибке БД

- [x] 3.1 В except-ветке допуска `manager.py`: для `sqlite3.IntegrityError` подставлять «Ошибка на уровне БД: нарушение целостности данных», остальные исключения — через `rejection_message` как сейчас; traceback остаётся в логе.

## 4. Тесты

- [x] 4.1 Интеграционный тест: две сделки на разных связках (инструментах) открываются подряд, цели `tp-1`/`tp-2` обеих сосуществуют в `targets`.
- [x] 4.2 Интеграционный тест миграции: сформировать базу v6 с живой сделкой и целями `tp-1`/`tp-2`, прогнать `initialize_schema` → `user_version == 7`, цели и статусы сохранены, следующая сделка открывается.
- [x] 4.3 Unit-тест в `tests/unit/trade_management/test_manager.py`: при подставленном `sqlite3.IntegrityError` причина имеет код `admission-error` и описание «Ошибка на уровне БД: нарушение целостности данных».

## 5. Документация

- [x] 5.1 Обновить `docs/database.md`: версия схемы 7, составной ключ `targets` (при необходимости — ER-диаграмму).
- [x] 5.2 Проверить тест на синхронизацию схемы с докой, если он есть; обновить ожидания.

## 6. Проверка

- [x] 6.1 Прогнать `.venv/bin/python -m pytest -q` и `.venv/bin/python -m ruff check .`.