# Схема БД (ER-диаграммы)

Каноническая схема живёт в `src/trade_journal/schema.py` (текущая версия —
`SCHEMA_VERSION = 9`). Данный файл — живое ER-описание той же схемы: обзорная
карта связей и диаграммы по предметным областям с колонками и типами, а также
комментарии о миграциях. Он сверяется с реальной схемой тестом
`tests/unit/trade_journal/test_schema_doc_sync.py` (сверка: таблицы, колонки,
`SCHEMA_VERSION`), поэтому при изменении БД его нужно править вместе со схемой,
иначе тест упадёт.

## Обзор: карта связей между областями

Схема разбита на 5 предметных областей; номер области указан в заголовке
таблицы. Детализация по каждому номеру приведена ниже отдельной диаграммой с
полным набором колонок.

```mermaid
erDiagram
    trades["Сделки (1)"]
    positions["Позиции (1)"]
    targets["Цели выхода (1)"]
    protection["Стоп-защита (1)"]
    instrument_names["Короткие имена контрактов (1)"]
    outbox["Исходящие команды (2)"]
    orders["Ордера (2)"]
    fills["Исполнения (2)"]
    account["Счёт (3)"]
    reservations["Резервы средств (3)"]
    market_inputs["Входные данные рынка (4)"]
    calculations["Расчёты стратегий (4)"]
    processed_signals["Обработанные сигналы (4)"]
    events["События (5)"]
    audit_exports["Аудит экспорта расчётов (5)"]
    export_state["Состояние экспорта (5)"]
    trades ||--o| positions : "1..1 — 0..1"
    trades ||--o{ outbox : "1 — 0..*"
    trades ||--o{ orders : "1 — 0..*"
    trades ||--o{ fills : "1 — 0..*"
    trades ||--o{ targets : "1 — 0..*"
    trades ||--o| protection : "1..1 — 0..1"
    trades ||--o{ reservations : "1 — 0..*"
    trades o|--o{ events : "0..1 — 0..*"
    trades o|--o{ processed_signals : "0..1 — 0..*"
    trades o|--o{ calculations : "0..1 — 0..*"
    trades }o--o| instrument_names : "ticker — короткое имя"
    outbox ||--o| orders : "1 — 0..1"
    outbox ||--o| protection : "1 — 0..1"
    outbox o|--o{ events : "0..1 — 0..*"
    outbox o|--o{ calculations : "0..1 — 0..*"
    orders ||--o{ fills : "1 — 0..*"
    orders ||--o{ fills : "1 — 0..* (command_id)"
    orders o|--o| protection : "0..1 — 0..1"
    orders o|--o{ events : "0..1 — 0..*"
    events o|--o{ calculations : "0..1 — 0..*"
    market_inputs ||--o{ calculations : "1 — 0..*"
    calculations ||--o| audit_exports : "1..1 — 0..1"
```

**Области:** 1 — Журнал сделок; 2 — Исполнение и команды брокеру;
3 — Счёт и средства; 4 — Аналитика стратегий; 5 — Аудит и экспорт.

Условные обозначения: `PK` — первичный ключ, `FK` — внешний ключ (связь показана
линией либо помечена как `→ обл. N (таблица)` — таблица из другой области),
`UK` — уникальное ограничение/индекс. В комментарии к колонке: значение на
русском, обязательность (`обяз.` / `может быть NULL`), `DEFAULT`, `CHECK`,
поведение внешнего ключа `ON DELETE`. Порядок колонок в каждом блоке
соответствует `CREATE TABLE` в `schema.py` (`SCHEMA_VERSION = 9`); типы —
`TEXT`/`INTEGER`. JSON-колонки перечислены с ссылкой `json-schemas.md#...` на
структуру документа (см. [JSON-схемы](json-schemas.md)). Ограничений длины строк
на уровне БД нет (все текстовые колонки `TEXT` без `LIMIT`/`VARCHAR(N)`).
Составные первичные ключи:
`targets (trade_id, target_id)` и `processed_signals (assignment_id, signal_id)`.

## 1. Журнал сделок

Карточка сделки: план, позиция, цели выхода и защитный стоп.

```mermaid
erDiagram
    trades["Сделки"] {
        TEXT trade_id PK "Уникальный id сделки; обяз."
        TEXT assignment_id "Id торгового поручения (присваивания); обяз."
        TEXT instrument_id "Тикер инструмента; обяз."
        TEXT signal_id "Id входного сигнала; обяз."
        TEXT side "Сторона сделки; обяз.; CHECK: BUY|SELL"
        TEXT plan_json "План сделки (JSON: json-schemas.md#plan_json); обяз."
        TEXT profile_json "Профиль торговли (JSON: json-schemas.md#profile_json); обяз."
        TEXT phase "Фаза сделки (OPEN|CLOSED|CANCELLED|...); обяз."
        INTEGER state_revision "Ревизия состояния; обяз.; DEFAULT 0; CHECK: >= 0"
        TEXT profile_state_json "Состояние профиля (JSON: json-schemas.md#profile_state_json); обяз.; DEFAULT {}"
        TEXT created_at "Дата открытия (UTC ISO8601); обяз."
        TEXT updated_at "Дата изменения (UTC ISO8601); обяз."
        TEXT price_step "Ценовой шаг контракта (v8); может быть NULL (исторические сделки)"
        TEXT step_cost "Стоимость шага контракта, руб. (v8); может быть NULL"
    }
    positions["Позиции"] {
        TEXT trade_id PK, FK "Сделка; ON DELETE CASCADE"
        TEXT side "Сторона позиции; обяз.; CHECK: BUY|SELL"
        INTEGER quantity "Размер позиции; обяз.; DEFAULT 0; CHECK: >= 0"
        TEXT average_price "Средняя цена входа; может быть NULL"
        TEXT realized_pnl "Реализованный PnL, брутто; обяз.; DEFAULT 0"
        TEXT fees "Накопленные комиссии; обяз.; DEFAULT 0"
        TEXT net_realized_pnl "Чистый реализованный PnL; обяз.; DEFAULT 0"
        TEXT updated_at "Дата изменения; обяз."
    }
    targets["Цели выхода"] {
        TEXT target_id PK "Обозначение цели (tp-1, tp-2); часть составного PK"
        TEXT trade_id PK, FK "Сделка; часть составного PK; ON DELETE CASCADE"
        INTEGER target_index "Порядковый номер цели; обяз.; CHECK: >= 0; UNIQUE(с trade_id)"
        TEXT price "Цена цели; обяз."
        INTEGER planned_quantity "Плановый объём; обяз.; DEFAULT 0; CHECK: >= 0"
        INTEGER filled_quantity "Исполненный объём; обяз.; DEFAULT 0; CHECK: >= 0"
        TEXT status "Статус цели; обяз."
    }
    protection["Стоп-защита"] {
        TEXT trade_id PK, FK "Сделка; ON DELETE CASCADE"
        TEXT confirmed_stop "Цена подтверждённого стопа; может быть NULL"
        TEXT pending_stop "Цена ожидающего стопа; может быть NULL"
        TEXT confirmed_order_id FK "Подтверждённый стоп-ордер; может быть NULL; ON DELETE SET NULL; → обл. 2 (orders)"
        TEXT pending_command_id FK "Команда стоп-ордера; может быть NULL; ON DELETE SET NULL; → обл. 2 (outbox)"
        TEXT updated_at "Дата изменения; обяз."
    }
    trades ||--o| positions : "1..1 — 0..1"
    trades ||--o{ targets : "1 — 0..*"
    trades ||--o| protection : "1..1 — 0..1"
    instrument_names["Короткие имена контрактов"] {
        TEXT ticker PK "Биржевой тикер (совпадает с trades.instrument_id); обяз."
        TEXT short_name "Короткое имя контракта для пользовательского вывода; обяз."
        TEXT updated_at "Дата изменения; обяз."
    }
```

Связи `trades` с областями 2–5 (исполнение, счёт, аналитика, аудит) показаны на
обзорной карте; на стороне дочерних таблиц соответствующие FK-колонки помечены
`→ обл. N`.

## 2. Исполнение и команды брокеру

Цепочка command → order → fill: исходящие команды брокеру, заявки и исполнения.

```mermaid
erDiagram
    outbox["Исходящие команды"] {
        TEXT command_id PK "Уникальный id команды брокеру; обяз."
        TEXT trade_id FK "Сделка; обяз.; ON DELETE CASCADE; → обл. 1 (trades)"
        TEXT payload_json "Тело команды (JSON: json-schemas.md#payload_json-outbox); обяз."
        TEXT status "Статус отправки; обяз."
        TEXT created_at "Дата создания; обяз."
        TEXT sent_at "Дата отправки; может быть NULL (не отправлена)"
    }
    orders["Ордера"] {
        TEXT order_id PK "Биржевой id ордера; обяз."
        TEXT trade_id FK "Сделка; обяз.; ON DELETE CASCADE; → обл. 1 (trades)"
        TEXT command_id FK, UK "Команда из outbox; обяз.; UNIQUE; ON DELETE RESTRICT"
        TEXT action_type "Тип действия; обяз."
        TEXT status "Статус ордера; обяз."
        INTEGER quantity "Заявленный объём; обяз.; CHECK: > 0"
        INTEGER filled_quantity "Исполненный объём; обяз.; DEFAULT 0; CHECK: >= 0"
        TEXT requested_price "Запрошенная цена (лимит); может быть NULL"
        TEXT created_at "Дата создания; обяз."
        TEXT updated_at "Дата изменения; обяз."
    }
    fills["Исполнения"] {
        TEXT fill_id PK "Id исполнения в журнале; обяз."
        TEXT order_id FK "Ордер; обяз.; ON DELETE RESTRICT"
        TEXT trade_id FK "Сделка; обяз.; ON DELETE RESTRICT; → обл. 1 (trades)"
        TEXT command_id FK "Команда (через ордер); обяз.; ON DELETE RESTRICT"
        TEXT execution_id UK "Биржевой id сделки; обяз.; UNIQUE"
        INTEGER quantity "Исполненный объём; обяз.; CHECK: > 0"
        TEXT price "Цена исполнения; обяз."
        TEXT fee "Комиссия за исполнение; обяз.; DEFAULT 0"
        TEXT executed_at "Время исполнения; обяз."
    }
    outbox ||--o| orders : "1 — 0..1"
    orders ||--o{ fills : "1 — 0..*"
    orders ||--o{ fills : "1 — 0..* (command_id)"
```

## 3. Счёт и средства

Учёт денег: баланс/эквити счёта и зарезервированные под сделки риск и ГО.

```mermaid
erDiagram
    account["Счёт"] {
        INTEGER account_id PK "Однорядная таблица; CHECK: = 1"
        TEXT balance "Баланс счёта, руб.; обяз."
        TEXT equity "Активы (эквити), руб.; обяз."
        TEXT realized_pnl "Реализованный PnL, брутто; обяз.; DEFAULT 0"
        TEXT fees "Суммарные комиссии; обяз.; DEFAULT 0"
        TEXT net_realized_pnl "Чистый реализованный PnL; обяз.; DEFAULT 0"
        TEXT updated_at "Дата изменения; обяз."
    }
    reservations["Резервы средств"] {
        TEXT reservation_id PK "Уникальный id резерва; обяз."
        TEXT trade_id FK "Сделка; обяз.; ON DELETE CASCADE; → обл. 1 (trades)"
        TEXT order_id FK, UK "Ордер под резервом; обяз.; UNIQUE; ON DELETE CASCADE; → обл. 2 (orders)"
        TEXT risk_amount "Текущий риск, руб.; обяз."
        TEXT margin_amount "Текущий ГО, руб.; обяз."
        TEXT original_risk_amount "Исходный риск (v3+); обяз."
        TEXT original_margin_amount "Исходный ГО (v3+); обяз."
        TEXT status "Статус резерва; обяз."
        TEXT created_at "Дата создания; обяз."
        TEXT updated_at "Дата изменения; обяз."
    }
```

## 4. Аналитика стратегий

Рыночные данные, результаты расчётов стратегий и отметки об обработке сигналов.

```mermaid
erDiagram
    market_inputs["Входные данные рынка"] {
        TEXT source_data_id PK "Id источника данных; обяз."
        TEXT kind "Вид данных (тик/бар/...); обяз."
        TEXT data_json "Данные (JSON: json-schemas.md#data_json-market_inputs); обяз."
        TEXT seed_json "Семя воспроизведения (JSON: json-schemas.md#seed_json-market_inputs); может быть NULL"
        TEXT available_at "Момент доступности данных; обяз."
        TEXT created_at "Дата создания; обяз."
    }
    calculations["Расчёты стратегий"] {
        TEXT calculation_id PK "Уникальный id расчёта; обяз."
        TEXT source_data_id FK "Входные данные; может быть NULL; ON DELETE RESTRICT"
        TEXT trade_id FK "Сделка; может быть NULL; ON DELETE SET NULL; → обл. 1 (trades)"
        TEXT command_id FK "Команда; может быть NULL; ON DELETE SET NULL; → обл. 2 (outbox)"
        TEXT event_id FK "Событие; может быть NULL; ON DELETE SET NULL; → обл. 5 (events)"
        TEXT assignment_id "Id присваивания; может быть NULL"
        TEXT signal_id "Id сигнала; может быть NULL"
        TEXT service_uid "Служебный id; может быть NULL"
        TEXT correlation_id "Id корреляции; может быть NULL"
        TEXT algorithm "Алгоритм (стратегия); обяз."
        TEXT algorithm_version "Версия алгоритма; обяз."
        TEXT input_json "Вход (JSON: json-schemas.md#input_json); обяз."
        TEXT steps_json "Шаги расчёта (JSON: json-schemas.md#steps_json); обяз."
        TEXT rounding_json "Параметры округления (JSON: json-schemas.md#rounding_json); обяз."
        TEXT output_json "Выход (JSON: json-schemas.md#output_json); обяз."
        TEXT outcome "Итог; обяз.; CHECK: ACCEPTED|REJECTED"
        TEXT reason "Причина решения; может быть NULL"
        TEXT created_at "Дата создания; обяз."
    }
    processed_signals["Обработанные сигналы"] {
        TEXT assignment_id PK "Id присваивания; часть составного PK"
        TEXT signal_id PK "Id сигнала; часть составного PK"
        TEXT trade_id FK "Сделка; может быть NULL; ON DELETE SET NULL; → обл. 1 (trades)"
        TEXT processed_at "Время обработки; обяз."
    }
    market_inputs ||--o{ calculations : "1 — 0..*"
```

## 5. Аудит и экспорт

Служебная трасса: лента событий сделок и состояние экспорта/аудита расчётов.

```mermaid
erDiagram
    events["События"] {
        INTEGER event_seq PK "Порядковый номер (автоинкремент); обяз."
        TEXT event_id UK "Стабильный id события; обяз.; UNIQUE"
        TEXT trade_id FK "Сделка; может быть NULL; ON DELETE SET NULL; → обл. 1 (trades)"
        TEXT order_id FK "Ордер; может быть NULL; ON DELETE SET NULL; → обл. 2 (orders)"
        TEXT command_id FK "Команда; может быть NULL; ON DELETE SET NULL; → обл. 2 (outbox)"
        TEXT event_type "Тип события; обяз."
        TEXT payload_json "Данные события (JSON: json-schemas.md#payload_json-events); обяз."
        TEXT occurred_at "Время события; обяз."
    }
    audit_exports["Аудит экспорта расчётов"] {
        TEXT calculation_id PK, FK "Расчёт; ON DELETE CASCADE; → обл. 4 (calculations)"
        TEXT exported_at "Время экспорта; обяз."
    }
    export_state["Состояние экспорта"] {
        INTEGER export_id PK "Однорядная таблица; CHECK: = 1"
        INTEGER required_revision "Требуемая ревизия; обяз.; DEFAULT 0; CHECK: >= 0"
        INTEGER exported_revision "Экспортированная ревизия; обяз.; DEFAULT 0; CHECK: >= 0"
        INTEGER audit_exported_revision "Экспортированная ревизия аудита; обяз.; DEFAULT 0; CHECK: >= 0"
        INTEGER failed_revision "Ревизия последнего сбоя; может быть NULL; CHECK: >= 0"
        TEXT last_error "Текст последней ошибки; может быть NULL"
        INTEGER audit_failed_revision "Ревизия последнего сбоя аудита; может быть NULL; CHECK: >= 0"
        TEXT audit_last_error "Текст последней ошибки аудита; может быть NULL"
        TEXT updated_at "Дата изменения; обяз."
    }
```

## Комментарии к ролям связей (по семантике `ON DELETE`)

- `events.trade_id` — `ON DELETE SET NULL`, поэтому сделка может быть
  удалена после события (0..1 — 0..*).
- `audit_exports` — это данные аудита экспорта точных расчётов, 1..1 к
  `calculations` с `ON DELETE CASCADE`.
- `targets` — первичный ключ составной `(trade_id, target_id)`: одинаковые
  текстовые обозначения целей (`tp-1`, `tp-2`) разрешены у разных сделок.
  Также есть `UNIQUE (trade_id, target_index)`.
- На карте связей видно, что `trades` — центральная точка: она связана почти
  со всеми областями; сами связи в детальных диаграммах областей 2–5 возникают
  на стороне дочерних таблиц (помечены `→ обл. N`).

## Версия 8: рублёвая эпоха сделок

В `trades` добавлены `price_step`/`step_cost` — снапшот факторов контракта на
момент открытия сделки (текст, `NULL` для исторических сделок до миграции
v7→v8). По ним reducer считает PnL в рублях
(`direction × (price − avg) / price_step × step_cost × quantity`); у сделок без
факторов PnL остаётся в «сырых» единицах. Миграция v7→v8 выполняется в
`initialize_schema` при открытии БД; если передан `initial_balance` (депозит
робота), счёт пересобирается с этого значения (re-baseline учётного баланса).

## Версия 9: короткие имена контрактов в базе

Добавлена таблица `instrument_names` — карта «биржевой тикер → короткое имя»
(`NGV6` → `NG-10.26`). Раньше карта жила только в памяти процесса: её строил
селектор инструментов и передавал в `Storage.set_names` → экспортёру, поэтому
`trades.instrument_id` содержал сырой тикер, а инструмент, открывший базу вне
работающего робота, не мог показать пользователю короткое имя.

Карта записывается идемпотентным обновлением при `set_names` (один раз за
запуск робота, лишних строк не создаёт). Короткое имя определяется селектором
инструментов, а не разбором тикера: тикер не позволяет однозначно восстановить
имя. Читать карту из базы может любой инструмент в режиме `mode=ro`, без сети
и без интерактивного выбора инструментов. Миграция v8→v9 только создаёт таблицу;
существующие строки не изменяются, а наполнение происходит при первом запуске
робота после обновления. Подробнее — в
[signal-pipeline.md](trade-management/signal-pipeline.md) и в
[storage-and-audit.md](trade-management/storage-and-audit.md).

## Как и когда обновлять

1. Внёс изменение в `CREATE TABLE` / `SCHEMA_VERSION` в `schema.py`?
2. Прогони `tests/unit/trade_journal/test_schema_doc_sync.py` — он сравнит
   таблицы и колонки из этого файла с реальной схемой и укажет расхождения.
3. Обнови обзорную карту и диаграмму соответствующей области — колонки, типы и
   связи.
4. Оформи изменениe через OpenSpec-workflow (delta-спеки в
   `openspec/changes/`), чтобы спека и БД не разошлись.