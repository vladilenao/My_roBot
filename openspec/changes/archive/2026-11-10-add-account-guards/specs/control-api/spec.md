## ADDED Requirements

### Requirement: Предохранители контура «Песочница» в API
Нормативный уровень: MUST. `GET /api/v1/sandbox` ДОЛЖЕН отдавать в `current.guards` список сработавших предохранителей; пустой список, если всё чисто. У предохранителя ДОЛЖНЫ быть:

- `kind`: `daily_loss` или `broker_rejects`;
- `since`;
- `until`: начало следующего торгового дня у `daily_loss`, `null` у `broker_rejects`;
- `detail`: у дневного убытка — `loss`, `limit`, `start_capital` десятичными строками и `partial_day`; у отказов — `count` и `reasons` (до трёх).

`_actions` ДОЛЖЕН содержать `guards-reset`, когда сработал `broker_rejects`.

`POST /api/v1/sandbox/guards/reset` с телом `{"guards": ["broker_rejects"]}` ДОЛЖЕН увеличивать поколение сброса в желаемом состоянии контура и записывать команду в журнал команд ядра. Ответы:

- `202` — поколение записано, тело — состояние песочницы;
- `400` — тело не объект, неизвестные поля, `guards` не непустой список известных видов;
- `409 NOT_RUNNING` — процесса контура нет;
- `409 GUARD_NOT_RESETTABLE` — в списке `daily_loss`: он снимается с новым торговым днём.

Сброс без сработавшего предохранителя ДОЛЖЕН приниматься и ничего не менять. Маршрут и схемы ДОЛЖНЫ быть описаны в `docs/api/openapi.yaml` версии 2.9.0.

#### Scenario: Сработавший предохранитель в состоянии
- **GIVEN** у песочницы сработал `broker_rejects` после трёх отказов
- **WHEN** приходит `GET /api/v1/sandbox`
- **THEN** `current.guards` содержит `broker_rejects` с `count = 3` и причинами, `_actions` содержит `guards-reset`

#### Scenario: Сброс отказов
- **GIVEN** сработал `broker_rejects`
- **WHEN** приходит `POST /api/v1/sandbox/guards/reset` с `{"guards": ["broker_rejects"]}`
- **THEN** ответ `202`, поколение сброса увеличено, на ближайшей границе тика `current.guards` пуст

#### Scenario: Дневной убыток командой не снимается
- **WHEN** приходит `POST /api/v1/sandbox/guards/reset` с `{"guards": ["daily_loss"]}`
- **THEN** ответ `409 GUARD_NOT_RESETTABLE`, поколение не меняется
