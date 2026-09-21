# Tasks — fix-duplicate-admission-and-bar-execution

## 1. Каноническая идентичность и дедупликация

- [x] 1.1 В `src/bot/trading_bot.py` (`_analyze`) безусловно собирать канонический
  `event_id` из `assignment.id`, `instrument.ticker`, `timeframe`, времени закрытия
  бара в МСК и направления; не сохранять `event_id`, возвращённый стратегией.
- [x] 1.2 Применить `BAR_TIME_TZ_OFFSET_HOURS` при рендере времени в `event_id`
  (время закрытия бара, МСК).
- [x] 1.3 В `src/trade_management/manager.py` (`_plan_entry`/`submit_plan`) сохранить
  `trade_id = decision.event_id`, а перед `INSERT INTO trades` добавить проверку
  существующего `trade_id`; при совпадении возвращать `duplicate-signal`, а не падать
  на PRIMARY KEY.
- [x] 1.4 Обновить `_REJECTION_MESSAGES["duplicate-signal"]` в
  `src/trade_management/models.py` на «дублирующий сигнал, сделка не взята в работу».
- [x] 1.5 Unit-тесты: две связки одной стратегии/ТФ с разными профилями создают две
  сделки с разными `trade_id`; повтор внутри связки даёт `duplicate-signal` без
  `UNIQUE constraint failed`; `event_id` содержит время МСК.

## 2. Уведомление о принятой в работу сделке

- [x] 2.1 Добавить необязательное поле `plan` в `SignalAdmission`
  (`src/trade_management/models.py`) и заполнять его в `_plan_entry` при успешном
  допуске.
- [x] 2.2 Добавить `report_entry_accepted(...)` в `ExecutionPort`
  (`src/execution/port.py`) и реализовать в `NotifyOnlyExecutionPort` и
  `BrokerExecutionPort`.
- [x] 2.3 Добавить метод нотификатора и формат текста: короткое имя контракта, ТФ,
  направление, объём, плановые вход/стоп/цели, формулировка «в работе, ждёт
  подтверждения» (без заявления об исполнении).
- [x] 2.4 В `_admit_candidate` (`src/bot/trading_bot.py`) вызывать порт при наличии
  `admission.plan`; уведомление не подавляет обычное уведомление о сигнале.
- [x] 2.5 Unit-тесты текста уведомления: содержит короткое имя, объём и уровни; не
  утверждает факт исполнения.

## 3. Адресная симуляция

- [x] 3.1 В точке сборки рантайма (`run.py`) нормализовать инструменты
  (`normalize_instrument`) один раз и передавать потребителям только `Instrument`.
- [x] 3.2 В `on_bar` использовать нормализованные инструменты; каждый сбой обработки
  бара и инструмента логировать, а не глотать (`run.py:182-183`).
- [x] 3.3 В `_track_addressed_bars` (`src/broker/journal_broker.py`) привести время
  активации заявки и время бара к единой базе (naive UTC) перед сравнением.
- [x] 3.4 В `run.py` дополнительно вызвать `storage.set_names(_instrument_names(...))`
  рядом с `broker.set_names(...)`.
- [x] 3.5 Unit-тесты: 4-кортежи нормализуются в `Instrument`; закрытый базовый бар
  активирует заявку «по следующему бару»; сравнение времени не поднимает
  `TypeError`; экспорт показывает короткое имя контракта.

## 4. Экспорт журнала

- [x] 4.1 Проверить, что карта коротких имён доходит до `StorageExporter`, и что
  `trade_event.csv`/`trade_summary.csv` содержат `Si-12.26`, а не `SiZ6`.

## 5. Проверка

- [x] 5.1 Обновить затронутые unit/snapshot-ожидания идентификаторов события.
- [x] 5.2 `.venv/bin/python -m pytest -q` — зелёный.
- [x] 5.3 `.venv/bin/python -m ruff check .` — без новых ошибок (пре-существующие в
  `tools/` и `openspec/_check_sync.py` допустимы).
