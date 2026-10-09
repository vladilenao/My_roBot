## 1. Схема и миграция v8

- [x] 1.1 В `src/trade_journal/schema.py` поднять `SCHEMA_VERSION = 8` и добавить `MIGRATE_V7_TO_V8_SQL`: `ALTER TABLE trades ADD COLUMN price_step TEXT;` и `ALTER TABLE trades ADD COLUMN step_cost TEXT;`
- [x] 1.2 Пробросить `initial_balance` в миграцию: сигнатура `initialize_schema(connection, *, initial_balance: str | None = None)`, а в v7→v8-ветке добавить re-baseline счёта (`balance = equity = initial_balance`, `realized_pnl = fees = net_realized_pnl = '0'` для `account_id = 1`) — только если `initial_balance` не `None`
- [x] 1.3 Добавить ветку перехода для каждой прежней версии (1…7) в цепочки `initialize_schema`, чтобы любая legacy-база доводилась до v8 (включая `_backfill_net_realized_pnl` как до v7)
- [x] 1.4 Пробросить `initial_deposit` из конфигурации через `connect()` и конструктор `Storage` (новый необязательный параметр, по умолчанию `None` — только схема без сброса счёта); актуализировать вызывающий код в `run.py`
- [x] 1.5 Обновить `docs/database.md`: версия схемы 8, политика единиц PnL (RUB/RAW) и re-baseline счёта

## 2. Снапшот факторов при создании сделки

- [x] 2.1 В `src/trade_management/manager.py::submit_plan` добавить параметры `price_step: Decimal | None = None`, `step_cost: Decimal | None = None` и записывать их в `INSERT INTO trades` (новые плейсхолдеры)
- [x] 2.2 В `admit_signal` передавать `meta.price_step`/`meta.step_cost` в `submit_plan`

## 3. Reducer: рублёвый расчёт

- [x] 3.1 В `src/trade_journal/reducer.py::apply_fill` и `apply_fill_with_trace` добавить опциональные `price_step`/`step_cost`; для reducing-филла с `price_step not in (None, 0)` считать `gross = direction × (price − avg) / price_step × step_cost × quantity`, иначе — прежний сырой `gross`; обновить `formula` в audit-трейсе
- [x] 3.2 В `ExecutionReducer.apply` расширить выборку из `trades` (рядом с `side`) до `price_step, step_cost` и пробросить их в `apply_fill_with_trace`

## 4. Экспорт: единицы PnL

- [x] 4.1 В `src/trade_journal/export.py` добавить в `trade_summary.csv` колонку «Ед. PnL» со значениями `RUB` (у сделки есть ненулевые факторы) / `RAW` (история без факторов); для `RAW`-строк `Result`/`MAE`/`MFE` сохраняют прежний расчёт
- [x] 4.2 Убедиться, что `Initial Risk` в карточке остаётся рублёвым и для рублёвых строк `Result`/`MAE`/`MFE` делят рубли на рубли (проверка тестами, без смены формулы)

## 5. Тесты и проверка

- [x] 5.1 Unit-тесты reducer в `tests/unit/trade_journal/test_trade_journal.py`: рублёвая формула (BR: шаг 10, стоимость шага 8.4), fallback при `price_step` `None`/`0`, отрицательный/положительный стороны, Decimal-округление
- [x] 5.2 Интеграционные тесты миграции v7→v8 в `tests/integration/test_trade_journal_storage.py`: колонки добавлены, legacy-строки не изменены, счёт сброшен к `initial_balance` (и НЕ сброшен, когда `initial_balance=None`)
- [x] 5.3 Интеграционные тесты персистентности в `tests/integration/test_trade_journal_reducer.py`: закрытие новой сделки сохраняет рублёвый Gross/Net в `positions` и `account`; повторный реплей воспроизводит те же значения
- [x] 5.4 Экспортный тест в `tests/integration/test_trade_summary.py`: для смешанной истории (RAW-сделка + RUB-сделка) колонка «Ед. PnL» корректна
- [x] 5.5 Прогнать `.venv/bin/python -m pytest -q` и `.venv/bin/python -m ruff check .` (допустимы прежние ошибки в `tools/` и `openspec/_check_sync.py`)