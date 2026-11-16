## ADDED Requirements

### Requirement: Ресурс контура «Счёт»
Нормативный уровень: MUST. Контур «Счёт» ДОЛЖЕН быть ресурсом `/api/v1/account` под ключом управления, с проверкой `Host`, ответами `application/json`, ошибками `application/problem+json`, `_links` и `_actions`, по образцу ресурса `/api/v1/sandbox`:

| Маршрут | Что делает |
|---|---|
| `GET /api/v1/account` | состояние: общее, целевое, текущее с фактами допуска, запуск, процесс, состав |
| `GET /api/v1/account/target` | целевое состояние |
| `PUT /api/v1/account/target` | `{"mode": "on"}` — запуск; `{"mode": "no_entries"}` или `{"mode": "close_all", "confirm": true}` — остановка |
| `GET /api/v1/account/bindings` | состав (до Р2 пуст) и все привязки конфигурации с отметкой, где каждая торгует |
| `POST /api/v1/account/bindings`, `POST /api/v1/account/bindings/remove` | до Р2 — `409 ACCOUNT_READ_ONLY` |
| `GET /api/v1/account/reconcile` | последний отчёт сверщика и `fee_probes` |
| `GET /api/v1/account/log?lines=N` | хвост лога контура |
| `GET /api/v1/account/positions`, `/trades`, `/performance` | ресурсы торговли контура; до Р2 пусты |

`current` ДОЛЖЕН содержать поля `current` песочницы и `admission`: `access_level` (`full_access` или `read_only`), `qualified_for_work_with`, `derivatives`, `tariff`, `limits`, `warnings`. В `bindings` у привязок конфигурации ДОЛЖНА появиться отметка `account`, когда привязка в составе «Счёта».

**Запуск.** `bindings` в теле `PUT /target` ДОЛЖНО давать `400`. Остальные коды — как у песочницы (`202`, `409` `ALREADY_RUNNING`, `STOPPING`, `NOT_RUNNING`, `CORE_NOT_READY`, `CONTOUR_DIR_BUSY`, `412 REVISION_CHANGED`).

**Остановка.** `close_all` без `confirm: true` ДОЛЖЕН отвечать `409 CONFIRM_REQUIRED` без записи ревизии. До Р2 оба режима остановки завершают процесс на ближайшем шаге и НЕ ДОЛЖНЫ трогать позиции, заявки и стоп-заявки на счёте.

Ресурсов `guards/reset` и `comparison` у «Счёта» до Р2 НЕТ: `404 NOT_FOUND`. `GET /api/v1` ДОЛЖЕН называть `account` среди ресурсов. OpenAPI ДОЛЖЕН описывать ресурс тегом «Счёт», версия 2.11.0.

#### Scenario: Запуск
- **GIVEN** ядро готово, заданы токен, номер счёта, бот и канал «Счёт»
- **WHEN** приходит `PUT /api/v1/account/target` с `{"mode": "on"}`
- **THEN** ответ `202` с `target.revision`, ядро запускает процесс `run.py --contour account`

#### Scenario: Состав до Р2
- **WHEN** приходит `POST /api/v1/account/bindings` с `{"bindings": ["future-ng-macd"]}`
- **THEN** ответ `409 ACCOUNT_READ_ONLY`, ревизия не меняется

#### Scenario: close_all без подтверждения
- **GIVEN** контур «Счёт» работает
- **WHEN** приходит `PUT /api/v1/account/target` с `{"mode": "close_all"}`
- **THEN** ответ `409 CONFIRM_REQUIRED`, процесс работает дальше

#### Scenario: Факты допуска
- **GIVEN** контур «Счёт» стартовал с токеном только на чтение
- **WHEN** приходит `GET /api/v1/account`
- **THEN** `current.admission.access_level = read_only`, `current.capital.rub` равен рублям на счёте, `current.account` — последние четыре символа номера, `current.orders_enabled = false`
