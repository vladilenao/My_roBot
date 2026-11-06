## MODIFIED Requirements

### Requirement: Ресурс контура «Песочница»
Нормативный уровень: MUST. Контур «Песочница» ДОЛЖЕН быть ресурсом `/api/v1/sandbox` под ключом управления, с проверкой `Host`, ответами `application/json`, ошибками `application/problem+json`, `_links` и `_actions`, `ETag` и `If-Match` по правилам эмуляции. `confirm` контуру песочницы НЕ НУЖЕН: денег на ней нет. Маршруты и схемы ДОЛЖНЫ быть описаны в `docs/api/openapi.yaml` версии 2.2.0.

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
- `422 BINDINGS_REJECTED` — список отказов: `id` и причина. Причины: «Привязки нет в конфигурации» или причина, по которой ядро не определило инструмент привязки (те же формулировки, что у отказа инструмента эмуляции). Запуск не начинается.

**Состав.** Добавление и снятие ДОЛЖНЫ отвечать `202` с новой ревизией или `200` без изменений (уже в составе, уже снята). Неизвестный `id` или привязка, инструмент которой не определён, ДОЛЖНЫ давать `422 BINDINGS_REJECTED`, при этом состав не меняется. Команды состава ДОЛЖНЫ приниматься и тогда, когда процесс не запущен: они меняют целевой состав для следующего запуска.

На работающем контуре с `orders = true` режим снятия ДОЛЖЕН действовать на сделки привязки:

- `no_entries` — привязка доводит свои сделки до выхода;
- `close_all` — её сделки закрываются.

Пока у снятой привязки есть сделки, она остаётся в `bindings` с `target = removed` и `current` `draining` или `closing`.

**Состояние.** `state` — то же перечисление, что у эмуляции: `waiting_command`, `starting`, `running`, `stopping_no_entries`, `stopping_close_all`, `stopped`, `failed`. `current` ДОЛЖЕН содержать:

- `applied_revision`;
- `orders_enabled` — режим заявок;
- `account` — последние четыре символа;
- `capital` — `rub`, `liquid_portfolio`;
- `positions` — инструмент коротким именем, лоты;
- `orders` и `stop_orders` — число на счёте;
- `trades` — число открытых сделок журнала;
- `orders_in_flight` — число незавершённых заявок журнала заявок;
- `frozen` — инструменты коротким именем;
- `last_reconcile_at`;
- `account_link`, `core_link`, `stream_link`;
- `reason`.

`bindings` — участники: `id`, инструмент привязки (короткое имя контракта, если определён), стратегия, таймфрейм, `target` (`active` или `removed`) и `current`: `warming`, `trading`, `draining`, `closing`, `rejected` с причиной или `watching` (режим только чтения).

**Привязки.** `GET /api/v1/sandbox/bindings` ДОЛЖЕН возвращать `members` — состав песочницы — и `available` — все привязки конфигурации. У каждой привязки — `id`, инструмент, стратегия, таймфрейм и `trades_in`:

- `emulation`, если инструмент привязки сейчас в составе эмуляции;
- `sandbox`, если привязка в составе песочницы.

#### Scenario: Запуск с составом
- **GIVEN** ядро готово, в конфигурации есть привязки `share-sber-macd-4h-triple` и `future-ng-macd`
- **WHEN** приходит `PUT /api/v1/sandbox/target` с `{"mode": "on", "bindings": ["share-sber-macd-4h-triple"]}`
- **THEN** ответ `202` с `target.revision`, в составе одна привязка с инструментом SBER, ядро запускает процесс песочницы

#### Scenario: Неизвестная привязка
- **WHEN** приходит `POST /api/v1/sandbox/bindings` с `{"bindings": ["share-xxx"]}`
- **THEN** ответ `422 BINDINGS_REJECTED` с причиной для `share-xxx`, ревизия не меняется

#### Scenario: Нет действующего контракта
- **GIVEN** у семейства привязки нет контракта с запасом до экспирации
- **WHEN** приходит `POST /api/v1/sandbox/bindings` с этой привязкой
- **THEN** ответ `422 BINDINGS_REJECTED` с причиной, как у отказа инструмента эмуляции, состав не меняется

#### Scenario: Снятие привязки с открытой сделкой
- **GIVEN** у привязки `future-ng-macd` открытая сделка
- **WHEN** приходит `POST /api/v1/sandbox/bindings/remove` с `{"bindings": ["future-ng-macd"], "mode": "no_entries"}`
- **THEN** ответ `202`, привязка в `bindings` с `target = removed` и `current = draining`, пока сделка не закрыта

#### Scenario: Отчёт сверки
- **GIVEN** последняя сверка нашла ручную позицию NG
- **WHEN** приходит `GET /api/v1/sandbox/reconcile`
- **THEN** ответ содержит время, `mismatch`, строку NG `position` и NG в `frozen`

#### Scenario: Капитал в состоянии
- **GIVEN** команда песочницы показала на счёте 1 000 000 ₽, у контура одна открытая сделка
- **WHEN** приходит `GET /api/v1/sandbox` после очередного шага
- **THEN** `current.capital.rub` равен рублям на счёте, `current.trades = 1`, `current.orders_enabled = true`, `current.account` — четыре последних символа номера
