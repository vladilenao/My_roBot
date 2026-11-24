## ADDED Requirements

### Requirement: Ресурс контура «Счёт Финам»
Нормативный уровень: MUST. Контур `finam` ДОЛЖЕН быть ресурсом `/api/v1/finam` под ключом управления. Это тот же ресурс, что `/api/v1/account`, собранный точкой сборки со своими частями:

| Маршрут | Что делает |
|---|---|
| `GET /api/v1/finam` | состояние: общее, целевое, текущее с фактами допуска, запуск, процесс, состав |
| `GET`, `PUT /api/v1/finam/target` | целевое состояние; запуск и остановка, `close_all` — только с `confirm: true` |
| `GET /api/v1/finam/bindings` | состав и привязки конфигурации |
| `POST /api/v1/finam/bindings`, `…/bindings/remove` | `409 ACCOUNT_READ_ONLY` |
| `GET /api/v1/finam/reconcile` | последний отчёт сверщика, схема сверки песочницы (без `fee_probes`) |
| `GET /api/v1/finam/log?lines=N` | хвост лога контура |
| `GET /api/v1/finam/positions`, `/trades`, `/performance`, `/account` | ресурсы торговли контура |

- **Чтение.** При `orders = false` команды состава и `PUT …/target` с `bindings` ДОЛЖНЫ отвечать `409 ACCOUNT_READ_ONLY` с текстом про `[contours.finam] orders = false`.
- **Маршруты.** Ресурс сам объявляет свои маршруты. Сравнения и сброса предохранителей у ресурса без сравнения и торговли нет — `404`.
- **Факты допуска в `current.admission`** — `access_level`, `limits`, `warnings` (схема `FinamAdmission`). Полей Т-Инвест `qualified_for_work_with`, `derivatives`, `tariff` у Финама нет.
- **`GET /api/v1`** ДОЛЖЕН называть `finam` среди ресурсов и давать сводку `finam`.
- **Поле `broker`** — в сводке и `GET` ресурса `account` и `finam`: `tinvest` или `finam`.
- **`account_link`** ДОЛЖЕН допускать `maintenance` — брокер в плановом окне обслуживания.
- **Прежние ресурсы.** Пути, поля, схемы и коды ресурсов `/api/v1/sandbox` и `/api/v1/account` НЕ ДОЛЖНЫ меняться, кроме добавленного `broker`.
- **OpenAPI** ДОЛЖЕН описывать ресурс тегом «Счёт Финам», версия 2.14.0; изменения только добавляют.

#### Scenario: Сводка в точке входа
- **WHEN** приходит `GET /api/v1` с ключом управления
- **THEN** в `_links` есть `finam` → `/api/v1/finam`, в ответе — сводка `finam` с `broker: finam`, у сводки `account` — `broker: tinvest`

#### Scenario: Состав только на чтение
- **WHEN** приходит `POST /api/v1/finam/bindings` с `{"bindings": ["future-si-ma-raw"]}`
- **THEN** ответ `409 ACCOUNT_READ_ONLY`, ревизия не меняется

#### Scenario: Сравнения у Финама нет
- **WHEN** приходит `GET /api/v1/finam/comparison`
- **THEN** ответ `404`

#### Scenario: Запуск
- **GIVEN** ядро готово, заданы секрет и номер счёта Финама, бот и канал Финама
- **WHEN** приходит `PUT /api/v1/finam/target` с `{"mode": "on"}`
- **THEN** ответ `202` с `target.revision`, ядро запускает процесс `run.py --contour finam`

#### Scenario: Пути «Счёта» прежние
- **WHEN** приходят прежние запросы к `/api/v1/account/*` и `/api/v1/sandbox/*`
- **THEN** ответы те же, что до изменения, плюс поле `broker` у «Счёта»
