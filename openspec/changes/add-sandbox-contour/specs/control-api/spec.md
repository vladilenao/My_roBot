## ADDED Requirements

### Requirement: Ресурс контура «Песочница»
Нормативный уровень: MUST. Контур «Песочница» ДОЛЖЕН быть ресурсом `/api/v1/sandbox` под ключом управления, с проверкой `Host`, ответами `application/json`, ошибками `application/problem+json`, `_links` и `_actions`, `ETag` и `If-Match` по правилам эмуляции. `confirm` контуру песочницы НЕ НУЖЕН: денег на ней нет.

| Маршрут | Что делает |
|---|---|
| `GET /api/v1/sandbox` | состояние: общее, целевое, текущее, запуск, процесс, состав |
| `GET /api/v1/sandbox/target` | целевое состояние |
| `PUT /api/v1/sandbox/target` | `{"mode": "on", "bindings": [...]}` — запуск; `{"mode": "no_entries"}` или `{"mode": "close_all"}` — остановка |
| `GET /api/v1/sandbox/bindings` | состав песочницы и все привязки конфигурации с отметкой, где каждая торгует |
| `POST /api/v1/sandbox/bindings` | `{"bindings": [...]}` — добавить в состав |
| `POST /api/v1/sandbox/bindings/remove` | `{"bindings": [...], "mode": "no_entries" \| "close_all"}` — убрать из состава |
| `GET /api/v1/sandbox/reconcile` | последний отчёт сверщика |
| `GET /api/v1/sandbox/log?lines=N` | хвост лога контура, как у эмуляции |

**Запуск.** Поле `bindings` НЕ ОБЯЗАТЕЛЬНО, пустой состав допустим. Ответы:

- `202` — состояние песочницы и `Location: /api/v1/sandbox`;
- `400` — тело не объект, неизвестные поля, `mode` не из трёх значений, `bindings` не список строк или передан вместе с режимом остановки;
- `409` — коды эмуляции: `ALREADY_RUNNING`, `STOPPING`, `NOT_RUNNING` (остановка без процесса), `CORE_NOT_READY`, `CONTOUR_DIR_BUSY`;
- `412 REVISION_CHANGED`;
- `422 BINDINGS_REJECTED` — список отказов: `id` и причина «Привязки нет в конфигурации»; запуск не начинается.

**Состав.** Добавление и снятие ДОЛЖНЫ отвечать `202` с новой ревизией или `200` без изменений (уже в составе, уже снята). Неизвестный `id` ДОЛЖЕН давать `422 BINDINGS_REJECTED`, при этом состав не меняется. Команды состава ДОЛЖНЫ приниматься и тогда, когда процесс не запущен: они меняют целевой состав для следующего запуска.

**Состояние.** `state` — то же перечисление, что у эмуляции: `waiting_command`, `starting`, `running`, `stopping_no_entries`, `stopping_close_all`, `stopped`, `failed`. `current` ДОЛЖЕН содержать:

- `applied_revision`;
- `account` — последние четыре символа;
- `capital` — `rub`, `liquid_portfolio`;
- `positions` — инструмент коротким именем, лоты;
- `orders` и `stop_orders` — число;
- `frozen` — инструменты коротким именем;
- `last_reconcile_at`;
- `account_link`, `core_link`;
- `reason`.

`bindings` — участники: `id`, инструмент, стратегия, таймфрейм, `target` (`active` или `removed`) и `current` (`watching` в С2).

**Привязки.** `GET /api/v1/sandbox/bindings` ДОЛЖЕН возвращать `members` — состав песочницы — и `available` — все привязки конфигурации. У каждой привязки — `id`, инструмент, стратегия, таймфрейм и `trades_in`:

- `emulation`, если инструмент привязки сейчас в составе эмуляции;
- `sandbox`, если привязка в составе песочницы.

#### Scenario: Запуск с составом
- **GIVEN** ядро готово, в конфигурации есть привязки `share-sber-macd-4h-triple` и `future-ng-macd`
- **WHEN** приходит `PUT /api/v1/sandbox/target` с `{"mode": "on", "bindings": ["share-sber-macd-4h-triple"]}`
- **THEN** ответ `202` с `target.revision`, в составе одна привязка, ядро запускает процесс песочницы

#### Scenario: Неизвестная привязка
- **WHEN** приходит `POST /api/v1/sandbox/bindings` с `{"bindings": ["share-xxx"]}`
- **THEN** ответ `422 BINDINGS_REJECTED` с причиной для `share-xxx`, ревизия не меняется

#### Scenario: Отчёт сверки
- **GIVEN** последняя сверка нашла ручную позицию NG
- **WHEN** приходит `GET /api/v1/sandbox/reconcile`
- **THEN** ответ содержит время, `mismatch`, строку NG-11.26 `position` 1 против 0 и NG-11.26 в `frozen`

#### Scenario: Капитал в состоянии
- **GIVEN** команда песочницы показала на счёте 1 000 000 ₽
- **WHEN** приходит `GET /api/v1/sandbox` после очередного шага
- **THEN** `current.capital.rub` равен 1 000 000, `current.account` — четыре последних символа номера

## MODIFIED Requirements

### Requirement: Ресурсы контуров
Нормативный уровень: MUST. Каждый вид торгового контура ДОЛЖЕН быть отдельным ресурсом API со своей схемой:

- эмуляция — `/api/v1/emulation`, один контур на робота;
- история — `/api/v1/history`, коллекция прогонов;
- песочница — `/api/v1/sandbox`, один контур на брокере на робота.

Общие части ДОЛЖНЫ быть общими схемами контракта: целевое состояние, текущее состояние, запуск, процесс, участник состава. Контур «Песочница» и будущий контур «Счёт» ДОЛЖНЫ строиться на них же; участником состава у них служит привязка, а не инструмент.

Маршруты `/api/v1/contours`, `/api/v1/contours/{id}` и все вложенные в них ДОЛЖНЫ быть удалены: запрос к ним ДОЛЖЕН получать `404` с кодом `NOT_FOUND` без перенаправления и без выполнения команды.

API ДОЛЖЕН называть состояние словами оператора:

| В API | В `core.sqlite3` и `status.json` |
| --- | --- |
| `target` — целевое состояние | желаемое |
| `target.mode = on` | «работает» |
| `target.mode = no_entries` / `close_all` | «остановлен» с этим режимом |
| `target.revision`, заголовок `ETag` | номер поколения |
| `current` — текущее состояние | фактическое из `status.json` |
| `current.applied_revision` | применённое поколение |

Имена `desired`, `actual`, `generation`, `applied_generation`, `running`, `stop_mode` НЕ ДОЛЖНЫ появляться в запросах и ответах API управления. Внутренние имена базы и `status.json` НЕ ДОЛЖНЫ меняться.

#### Scenario: Прежний маршрут
- **WHEN** приходит `POST /api/v1/contours` с `{"kind": "emulation"}` и верным ключом
- **THEN** ответ `404` с кодом `NOT_FOUND`, торговля не начинается, целевое состояние не меняется

#### Scenario: Слова оператора в ответе
- **GIVEN** эмуляция торгует, в `core.sqlite3` поколение 3
- **WHEN** приходит `GET /api/v1/emulation`
- **THEN** ответ содержит `target = {"mode": "on", "revision": 3}` и `current.applied_revision`, а ключей `desired`, `actual` и `generation` в нём нет

#### Scenario: Песочница на общих схемах
- **WHEN** приходит `GET /api/v1/sandbox`
- **THEN** `target`, `current.applied_revision`, `launch` и `process` устроены так же, как у эмуляции, а участники состава — привязки

### Requirement: Точка входа API
Нормативный уровень: MUST. `GET /api/v1` ДОЛЖЕН принимать ключ управления или ключ экспорта и отвечать при любом состоянии ядра. Ответ ДОЛЖЕН содержать:

- `version` — версию робота;
- `core.state` — состояние старта ядра;
- `emulation` — `state`, число инструментов и открытых позиций; `null` для ключа экспорта;
- `history` — число идущих и ждущих прогонов и `max_history_runs`; `null` для ключа экспорта;
- `sandbox` — `state`, число привязок, число замороженных инструментов и итог последней сверки; `null` для ключа экспорта;
- `live` — `null`, пока контур «Счёт» не реализован;
- `_links` — ссылки на разделы, разрешённые ключу: для ключа управления — `core`, `emulation`, `history`, `sandbox`, `docs`; для ключа экспорта — `market-data` (`/api/v1/market-data/source`), `clusters-report`, `docs`.

Без ключа или с неверным ключом — `401`.

#### Scenario: Ключ управления
- **WHEN** приходит `GET /api/v1` с ключом управления
- **THEN** ответ `200` со сводкой эмуляции, истории и песочницы и ссылками на ядро, эмуляцию, историю и песочницу, без ссылок на экспорт

#### Scenario: Ключ экспорта
- **WHEN** приходит `GET /api/v1` с ключом экспорта
- **THEN** ответ `200`, `emulation`, `history` и `sandbox` равны `null`, ссылки — только на рыночные данные, отчёт кластеров и Swagger

#### Scenario: Ядро ещё не готово
- **GIVEN** старт ядра упал и ждёт повтора
- **WHEN** приходит `GET /api/v1` с ключом управления
- **THEN** ответ `200` с `core.state = retrying`, сводка эмуляции, истории и песочницы показана
