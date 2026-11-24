## ADDED Requirements

### Requirement: Ресурс контура «Счёт Финам»
Нормативный уровень: MUST. Контур `finam` ДОЛЖЕН быть ресурсом `/api/v1/finam` под ключом управления, зеркалом `/api/v1/account` с теми же схемами ответов и кодами:

| Маршрут | Что делает |
|---|---|
| `GET /api/v1/finam` | состояние: общее, целевое, текущее с фактами допуска, запуск, процесс, состав |
| `GET`, `PUT /api/v1/finam/target` | целевое состояние; запуск и остановка, `close_all` — только с `confirm: true` |
| `GET /api/v1/finam/bindings` | состав и привязки конфигурации |
| `POST /api/v1/finam/bindings`, `…/bindings/remove` | `409 ACCOUNT_READ_ONLY` |
| `GET /api/v1/finam/reconcile` | последний отчёт сверщика; `fee_probes` — `null` |
| `GET /api/v1/finam/log?lines=N` | хвост лога контура |
| `GET /api/v1/finam/positions`, `/trades`, `/performance`, `/account` | ресурсы торговли контура |

В этой версии команды состава и `PUT …/target` с `bindings` ДОЛЖНЫ отвечать `409 ACCOUNT_READ_ONLY` с текстом про `[contours.finam] orders = false`. Сравнения `/api/v1/finam/comparison` и сброса предохранителей `/api/v1/finam/guards/reset` нет — `404`.

`GET /api/v1` ДОЛЖЕН называть `finam` среди ресурсов и давать сводку `finam` рядом с `account`. Сводка и `GET` ресурса `account` и `finam` ДОЛЖНЫ содержать `broker`: `tinvest` у `account`, `finam` у `finam`. Значение `account_link` ДОЛЖНО допускать `maintenance` — брокер в плановом окне обслуживания.

Пути, поля и коды ресурсов `/api/v1/sandbox` и `/api/v1/account` НЕ ДОЛЖНЫ меняться, кроме добавленного `broker`. OpenAPI ДОЛЖЕН описывать ресурс тегом «Счёт Финам», версия 2.14.0; изменения только добавляют.

#### Scenario: Сводка в точке входа
- **WHEN** приходит `GET /api/v1` с ключом управления
- **THEN** в `_links` есть `finam` → `/api/v1/finam`, в ответе — сводка `finam` с `broker: finam`, у сводки `account` — `broker: tinvest`

#### Scenario: Состав только на чтение
- **WHEN** приходит `POST /api/v1/finam/bindings` с `{"bindings": ["future-si-ma-raw"]}`
- **THEN** ответ `409 ACCOUNT_READ_ONLY`, ревизия не меняется

#### Scenario: Запуск
- **GIVEN** ядро готово, заданы секрет и номер счёта Финама, бот и канал Финама
- **WHEN** приходит `PUT /api/v1/finam/target` с `{"mode": "on"}`
- **THEN** ответ `202` с `target.revision`, ядро запускает процесс `run.py --contour finam`

#### Scenario: Пути «Счёта» прежние
- **WHEN** приходят прежние запросы к `/api/v1/account/*` и `/api/v1/sandbox/*`
- **THEN** ответы те же, что до изменения, плюс поле `broker` у «Счёта»
