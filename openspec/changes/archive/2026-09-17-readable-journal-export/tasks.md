# Tasks: readable-journal-export

## 1. Проекции

- [x] 1.1 Переопределить `JOURNAL_COLUMNS`/`POSITIONS_COLUMNS` и русские `JOURNAL_HEADERS`/`POSITIONS_HEADERS` на читаемые наборы колонок в `src/trade_journal/export.py`
- [x] 1.2 Добавить карту тикер→короткое имя в `CsvExporter` (`set_names`, использование в `_display_contract`)
- [x] 1.3 Переписать ленту: время в МСК (`_msk`), сторона словами, стратегия+ТФ из `trade_id`, описание события фразами из `event_type`+`action_type`+payload (`_describe_event`), Кол-во/Цена/Комиссия/Причина из payload и `orders`
- [x] 1.4 Расширить карточку: стоп из `protection`, средняя цена/время выхода и причина из `fills`/`events` (`_closing_fills`, `_exit_reasons`), фактический риск из `reservations` (`_entry_risk`), ГО из метаданных контракта
- [x] 1.5 Округлять денежные и маржинальные поля до копеек (`_money`)
- [x] 1.6 Убрать `export_revision` из проекций CSV (версия остаётся в `export_state`)

## 2. Проброс имён

- [x] 2.1 Добавить `Storage.set_names(names)` → `CsvExporter.set_names()` в `src/trade_journal/storage.py`
- [x] 2.2 В `run.py` передать `storage.set_names(_instrument_names(instruments))`

## 3. Тесты

- [x] 3.1 Перевести тесты проекций (`tests/integration/test_trade_journal_export.py`) с проверки ревизий на проверку читаемого содержимого (журнал/карточка, отставание при ошибке записи)
- [x] 3.2 Прогнать `pytest tests` и `ruff` — зелёный набор
- [x] 3.3 Проверить вывод на реальной БД (демо-экспорт: читаемые строки ленты и карточки)