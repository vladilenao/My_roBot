# Хранилище сделок, CSV-экспорт и аудит

В режиме торговли (`[trading]`) робот хранит **всё** состояние и события в одном
SQLite-файле. Это единственный источник истины. Пользовательские CSV и файл аудита
— автоматические проекции базы: их можно читать, экспортировать и открывать в
Excel, но робот **никогда не читает их обратно для восстановления** и не использует
ручные правки в них как торговое состояние.

## 1. SQLite — источник истины

| Ключ `[trading]` | По умолчанию | Назначение |
|---|---|---|
| `database_file` | `trades.sqlite3` | Файл базы рядом с бинарником/в приложении (`app_dir`). |
| `journal_file` | `trade_journal.csv` | Пользовательская лента событий (проекция базы). |
| `positions_file` | производное от имени ленты | Карточки позиций (проекция базы). |
| `clearing_times` | `["14:05", "19:00"]` | Расписание клиринга (СНИМОК в ленте). |

Особенности:

- Соединение одно (unit-of-work), `foreign_keys=ON`, режим журнала **WAL**,
  `busy_timeout` 5 секунд; схемы известна только приложению (`user_version`).
- Одно подтверждённое исполнение атомарно обновляет позицию, счёт, резерв,
  цели и пишет событие — частичных состояний нет; повторное событие идемпотентно.
- Путь базы не может совпадать с путём экспортируемых CSV или аудита — запуск
  с такими настройками отклоняется.
- Старые CSV `trade_journal.csv`/`positions.csv` из прежних версий **не
  импортируются**: перед первым автоэкспортом новой базы они сохраняются под
  свободным именем `.legacy.<timestamp>` и не участвуют в торговле.

Поведение при сбоях:

- Нарушение целостности схемы или неизвестная версия → торговый старт
  останавливается с диагностикой (не «пустой портфель»).
- Не удалось записать решение транзакцией → увеличение позиции не отправляется
  брокеру (отправка команды отделена от фиксации намерения).
- Сбой базы/экспорта не снимает уже подтверждённый защитный стоп.

## 2. CSV: journal.csv и positions.csv

После каждой торговой транзакции и при старте робот экспортирует оба файла из
одного согласованного снимка базы (одна версия экспорта `export_revision` в обоих
файлах). Каждый файл заменяется атомарно (временный файл + `os.replace`), поэтому
файл либо старый целый, либо новый целый.

- `journal.csv` — лента событий: `export_revision`, `event_seq`, `occurred_at`,
  `event_type`, `trade_id`, `assignment_id`, `instrument_id`, `command_id`,
  `order_id`, `payload`.
- `positions.csv` — карточки: `export_revision`, `position_id` (= `trade_id`),
  профиль/фаза, сторона, остаток, средняя цена, реализованный PnL (валовой/чистый)
  и накопленные комиссии.

Что делать, если файл «занят» (типично для Windows):

1. Программа (например, Excel) открыла `positions.csv` и блокирует файл.
2. SQLite продолжает вести торговлю — сделка не теряется, ошибка и отставшая
   ревизия фиксируются в `export_state`/логах.
3. Как только файл освободится, экспорт повторится по более свежей ревизии сам.
4. Если файлы удалили вручную — они восстановятся при следующем экспорте.

CSV никогда не влияет на допуск торговых решений; вручную править файлы
бессмысленно для робота (полный `csv` закрыт, изменения перезапишутся снимком).

## 3. Аудит: trade_audit.log

Все вычисления, влияющие на торговое решение (сигнал, фильтр, план, размер,
сопровождение, PnL/комиссии, клиринг), сохраняются отдельными трассами в SQLite
(`calculations` + неизменные окна входных данных и seed индикаторов) и выводятся в
отдельный ротируемый файл — независимо от уровня `bot_debug.log`.

| Ключ `[logging]` | По умолчанию | Назначение |
|---|---|---|
| `audit_file` | `trade_audit.log` | JSONL-трассы расчётов (по одной строке на расчёт). |
| `audit_max_bytes` | 10485760 | Ротация по размеру. |
| `audit_backup_count` | 5 | Число архивов `trade_audit.log.N`. |

Строка трассы (`AuditExporter`) содержит: `calculation_id`, `source_data_id`,
связки (`trade_id`, `command_id`, `event_id`, `assignment_id`, `signal_id`,
`service_uid`, `correlation_id`), `algorithm`+`algorithm_version`, `inputs` (с
единицами), `steps` (формула/операнды/промежуточные числа), `rounding`, `result`,
`outcome`, `reason`, `created_at`.

- INFO `bot_debug.log` фиксирует принятые планы, команды и исполнения; подробные
  расчёты — в SQLite и `trade_audit.log`.
- Каждый расчёт экспортируется **ровно один раз** (флаг `audit_exports`);
  сбой записи не удаляет трассу и повторяется по устойчивому ID.
- Секреты не пишутся; в пользовательских полях контракт — короткое имя
  (`NG-10.26`), времена — МСК.

## 4. Проверяемый пример аудита

Ниже — реальные трассы, полученные из боевых модулей (`portfolio/risk.py`,
`trade_management/profiles/levels_rr.py`) и повторяемые одним тестом:
`tests/unit/trade_management/test_docs_audit_example.py` (те же числа ожидаются
независимо атомарными утверждениями).

Успешный вход — план `levels_rr` на E=100, support=97, tick=1, buffer=1
(та же геометрия, что в `profiles.md`): разрешён вход, стоп 96, цели 104/108.

```json
{"algorithm":"profile.levels_rr.plan","outcome":"ACCEPTED","reason":"planned",
 "inputs":{"signal_price":{"value":100.0,"unit":"price"},
           "market":{"value":{"support":97,"resistance":103,"price_step":1,"entry":100},"unit":"local-market-inputs"},
           "parameters":{"value":{"buffer_ticks":1,"target_R":[1,2],"shares":[0.5,0.5],"max_adds":0},"unit":"profile-parameters"}},
 "result":{"value":{"entry":"100.0","stop":"96","targets":[["tp-1","104","0.5"],["tp-2","108","0.5"]]},"unit":"trade-plan"},
 "formula":"profile parameters + signal + local market inputs -> plan or rejection"}
```

Отказ добора — портфельный sizing: текущая LONG-позиция 2 контракта (стоп 96)
уже использует 840 ₽ бюджета, третий контракт добавил бы 420 ₽ и превысил
бюджет 1000 ₽ → `result = 0` и `REJECTED`, а не «усреднение вниз».

```json
{"algorithm":"portfolio.maximum_additional_quantity","outcome":"REJECTED",
 "reason":"largest-permitted-integer-quantity","result":{"value":0,"unit":"contracts"},
 "inputs":{"balance":{"value":1000,"unit":"RUB"},"equity":{"value":1000,"unit":"RUB"},
           "budget_base":{"value":1000,"unit":"RUB"},
           "entry_price":{"value":100,"unit":"price"},"stop_price":{"value":96,"unit":"price"},
           "price_step":{"value":1,"unit":"price"},"step_cost":{"value":100,"unit":"RUB/tick"},
           "entry_fee":{"value":0,"unit":"RUB/contracts"},"exit_cost":{"value":0,"unit":"RUB/contracts"},
           "slippage":{"value":20,"unit":"RUB/contracts"},"margin":{"value":0,"unit":"RUB/contracts"}},
 "formula":"largest q satisfying trade/instrument/group/portfolio risk and margin limits"}
```

## 5. Проверка на macOS и Windows

- macOS: WAL-файлы (`trades.sqlite3-wal/-shm`) создаются рядом с базой; при
  закрытии приложения корректно (checkpoint+close). Проверен автоэкспорт после
  транзакций и при старте, восстановление удалённых CSV, блокировка файла
  (открытый CSV → отставшая ревизия и повторный экспорт после освобождения),
  перенос легаси-файлов в `.legacy.<timestamp>`.
- Windows: блокировка файла другой программой — штатный сценарий экспортёра
  (`os.replace` завершается ошибкой, `busy_timeout`, ревизия отстаёт, экспорт
  повторяется). CSV и аудит не участвуют в торговом допуске, поэтому блокировка
  не останавливает торговлю. Тест блокировки покрыт интеграционно:
  `tests/integration/test_trade_journal_export.py`.

## 6. Связанные тесты

- Хранилище/схема: `tests/integration/test_trade_journal_storage.py`,
  `tests/integration/test_trade_journal_recovery.py`.
- Автоэкспорт CSV и блокировка: `tests/integration/test_trade_journal_export.py`.
- Outbox/идемпотентность: `tests/integration/test_trade_journal_outbox.py`.
- Резервы и конкуренция: `tests/integration/test_trade_journal_reservations.py`.
- Reducer: `tests/integration/test_trade_journal_reducer.py`.
- Жизненный цикл сделки по четырём политикам:
  `tests/integration/test_trade_management_profile_lifecycle.py`.
- Воспроизводимые трассы раздела 4:
  `tests/unit/trade_management/test_docs_audit_example.py`.