## MODIFIED Requirements

### Requirement: Ресурс контура «Счёт»
Нормативный уровень: MUST. Контур «Счёт» ДОЛЖЕН быть ресурсом `/api/v1/account` под ключом управления, с проверкой `Host`, ответами `application/json`, ошибками `application/problem+json`, `_links` и `_actions`, по образцу ресурса `/api/v1/sandbox`:

| Маршрут | Что делает |
|---|---|
| `GET /api/v1/account` | состояние: общее, целевое, текущее с фактами допуска, запуск, процесс, состав |
| `GET /api/v1/account/target` | целевое состояние |
| `PUT /api/v1/account/target` | `{"mode": "on", "bindings": [...]}` — запуск; `{"mode": "no_entries"}` или `{"mode": "close_all", "confirm": true}` — остановка |
| `GET /api/v1/account/bindings` | состав «Счёта» и все привязки конфигурации с отметкой, где каждая торгует |
| `POST /api/v1/account/bindings` | `{"bindings": [...]}` — добавить в состав |
| `POST /api/v1/account/bindings/remove` | `{"bindings": [...], "mode": "no_entries"}` или `{"bindings": [...], "mode": "close_all", "confirm": true}` — убрать из состава |
| `POST /api/v1/account/guards/reset` | `{"guards": ["broker_rejects"]}` — снять предохранитель «отказы подряд» |
| `GET /api/v1/account/reconcile` | последний отчёт сверщика и `fee_probes` |
| `GET /api/v1/account/comparison` | сравнение трёх контуров по сигналам привязок «Счёта» |
| `GET /api/v1/account/log?lines=N` | хвост лога контура |
| `GET /api/v1/account/positions`, `/trades`, `/performance` | ресурсы торговли контура |

`current` ДОЛЖЕН содержать поля `current` песочницы (включая `guards`) и `admission`: `access_level` (`full_access` или `read_only`), `qualified_for_work_with`, `derivatives`, `tariff`, `limits`, `warnings`. В `bindings` у привязок конфигурации ДОЛЖНА быть отметка `account`, когда привязка в составе «Счёта».

**Состав.** Запуск с `bindings` и добавление привязки ДОЛЖНЫ принимать только привязки, которые в момент команды стоят в составе песочницы с `target = active`. Иначе — `422 BINDINGS_REJECTED`, причина «Привязки нет в составе песочницы: путь эмуляция → песочница → счёт», состав не меняется. Остальные проверки и коды — как у песочницы (неизвестная привязка, нет инструмента, `412 REVISION_CHANGED`). При `[contours.account] orders = false` команды состава ДОЛЖНЫ отвечать `409 ACCOUNT_READ_ONLY`.

**Подтверждение.** `PUT …/target` с `mode = close_all` и `POST …/bindings/remove` с `mode = close_all` без `confirm: true` ДОЛЖНЫ отвечать `409 CONFIRM_REQUIRED` без записи ревизии. Запуск, добавление привязки, `no_entries` и сброс предохранителя подтверждения НЕ ТРЕБУЮТ; `confirm` в их теле допустим и не влияет. `confirm` не логического типа — `400`.

**Остановка.** При `orders = false` оба режима остановки завершают процесс и НЕ ДОЛЖНЫ трогать позиции, заявки и стоп-заявки на счёте. При `orders = true` — как у песочницы: `no_entries` — сделки доживают со стопом у брокера, `close_all` — стоп снимается, сделки закрываются циклом выхода.

**Предохранители.** `POST …/guards/reset` — как у песочницы: «дневной убыток» командой не снимается (`409 GUARD_NOT_RESETTABLE`), без процесса — `409 NOT_RUNNING`.

Остальные коды — как у песочницы (`202`, `409` `ALREADY_RUNNING`, `STOPPING`, `NOT_RUNNING`, `CORE_NOT_READY`, `CONTOUR_DIR_BUSY`, `412 REVISION_CHANGED`). `GET /api/v1` ДОЛЖЕН называть `account` среди ресурсов. OpenAPI ДОЛЖЕН описывать ресурс тегом «Счёт», версия 2.12.0.

#### Scenario: Запуск
- **GIVEN** ядро готово, заданы токен, номер счёта, бот и канал «Счёт»
- **WHEN** приходит `PUT /api/v1/account/target` с `{"mode": "on"}`
- **THEN** ответ `202` с `target.revision`, ядро запускает процесс `run.py --contour account` с прежним составом

#### Scenario: Запуск с привязкой из песочницы
- **GIVEN** `orders = true`, привязка `future-si-ma-raw` в составе песочницы
- **WHEN** приходит `PUT /api/v1/account/target` с `{"mode": "on", "bindings": ["future-si-ma-raw"]}`
- **THEN** ответ `202` без подтверждения, в составе «Счёта» одна привязка, ядро запускает процесс `run.py --contour account`

#### Scenario: Привязка не из песочницы
- **GIVEN** `orders = true`, привязки `future-br-ma` нет в составе песочницы
- **WHEN** приходит `POST /api/v1/account/bindings` с `{"bindings": ["future-br-ma"]}`
- **THEN** ответ `422 BINDINGS_REJECTED` с причиной «Привязки нет в составе песочницы», ревизия не меняется

#### Scenario: Состав до Р2
- **GIVEN** `orders = false`
- **WHEN** приходит `POST /api/v1/account/bindings` с `{"bindings": ["future-si-ma-raw"]}`
- **THEN** ответ `409 ACCOUNT_READ_ONLY`, ревизия не меняется

#### Scenario: close_all без подтверждения
- **GIVEN** контур «Счёт» работает
- **WHEN** приходит `PUT /api/v1/account/target` с `{"mode": "close_all"}` или `POST /api/v1/account/bindings/remove` с `{"bindings": ["future-si-ma-raw"], "mode": "close_all"}`
- **THEN** ответ `409 CONFIRM_REQUIRED`, ревизия не меняется, сделки не закрываются

#### Scenario: Снятие с no_entries
- **GIVEN** у привязки «Счёта» открытая сделка
- **WHEN** приходит `POST /api/v1/account/bindings/remove` с `{"bindings": ["future-si-ma-raw"], "mode": "no_entries"}`
- **THEN** ответ `202` без подтверждения, привязка `draining`, сделка доживает со стопом у брокера

#### Scenario: Факты допуска
- **GIVEN** контур «Счёт» стартовал с `orders = true` и полным доступом токена
- **WHEN** приходит `GET /api/v1/account`
- **THEN** `current.admission.access_level = full_access`, `current.capital.rub` равен рублям на счёте, `current.account` — последние четыре символа номера, `current.orders_enabled = true`
