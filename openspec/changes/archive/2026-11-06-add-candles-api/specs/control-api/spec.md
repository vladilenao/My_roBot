## MODIFIED Requirements

### Requirement: Доступ к управлению
Нормативный уровень: MUST. Маршруты управления — `/api/v1/core`, `/api/v1/emulation…`, `/api/v1/history…` и `/api/v1/candles…` — ДОЛЖНЫ требовать ключ `ROBOT_CONTROL_TOKEN` в заголовке `Authorization: Bearer` и НЕ ДОЛЖНЫ принимать его в адресе запроса или cookie. Точка входа `GET /api/v1` ДОЛЖНА принимать любой из двух ключей. Ключ экспорта рыночных данных НЕ ДОЛЖЕН давать доступ к управлению, а ключ управления — к экспорту. Маршруты управления ДОЛЖНЫ отклонять запрос, если заголовок `Host` не равен `127.0.0.1`, `localhost` или `[::1]` с портом сервера. Сервер НЕ ДОЛЖЕН отдавать заголовки CORS. `ROBOT_CONTROL_TOKEN` ДОЛЖЕН быть в `secrets` журнала и НЕ ДОЛЖЕН печататься нигде.

#### Scenario: Ключ экспорта для управления
- **WHEN** команда запуска приходит с ключом `MARKET_DATA_EXPORT_TOKEN`
- **THEN** ответ `401` с кодом `UNAUTHORIZED`, торговля не начинается

#### Scenario: Чужой Host
- **WHEN** команда запуска приходит с верным ключом, но с заголовком `Host: evil.example:8101`
- **THEN** ответ `403`, торговля не начинается

#### Scenario: Ключ в адресе
- **WHEN** ключ передан параметром адреса, а заголовка `Authorization` нет
- **THEN** ответ `401`

#### Scenario: Ключ экспорта для свечей
- **WHEN** `GET /api/v1/candles/NG-12.26/15m` приходит с ключом `MARKET_DATA_EXPORT_TOKEN`
- **THEN** ответ `401` с кодом `UNAUTHORIZED` в `application/problem+json`

### Requirement: Точка входа API
Нормативный уровень: MUST. `GET /api/v1` ДОЛЖЕН принимать ключ управления или ключ экспорта и отвечать при любом состоянии ядра. Ответ ДОЛЖЕН содержать:

- `version` — версию робота;
- `core.state` — состояние старта ядра;
- `emulation` — `state`, число инструментов и открытых позиций; `null` для ключа экспорта;
- `history` — число идущих и ждущих прогонов и `max_history_runs`; `null` для ключа экспорта;
- `sandbox` — `state`, число привязок, число замороженных инструментов и итог последней сверки; `null` для ключа экспорта;
- `live` — `null`, пока контур «Счёт» не реализован;
- `_links` — ссылки на разделы, разрешённые ключу: для ключа управления — `core`, `emulation`, `history`, `sandbox`, `candles`, `docs`; для ключа экспорта — `market-data` (`/api/v1/market-data/source`), `clusters-report`, `docs`.

Без ключа или с неверным ключом — `401`.

#### Scenario: Ключ управления
- **WHEN** приходит `GET /api/v1` с ключом управления
- **THEN** ответ `200` со сводкой эмуляции, истории и песочницы и ссылками на ядро, эмуляцию, историю, песочницу и ряды свечей (`/api/v1/candles`), без ссылок на экспорт

#### Scenario: Ключ экспорта
- **WHEN** приходит `GET /api/v1` с ключом экспорта
- **THEN** ответ `200`, `emulation`, `history` и `sandbox` равны `null`, ссылки — только на рыночные данные, отчёт кластеров и Swagger

#### Scenario: Ядро ещё не готово
- **GIVEN** старт ядра упал и ждёт повтора
- **WHEN** приходит `GET /api/v1` с ключом управления
- **THEN** ответ `200` с `core.state = retrying`, сводка эмуляции, истории и песочницы показана

### Requirement: Ошибки API управления
Нормативный уровень: MUST. Ответ с ошибкой на точке входа и на маршрутах управления (`/api/v1/core`, `/api/v1/emulation…`, `/api/v1/history…`, `/api/v1/candles…`) ДОЛЖЕН быть `application/problem+json` (RFC 9457) с полями:

- `type` — `urn:my-robot:problem:<код>`, код в нижнем регистре через дефис;
- `title` — короткое название ошибки;
- `status` — код HTTP;
- `code` — код ошибки: `INVALID_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN_HOST`, `NOT_FOUND`, `REPORT_NOT_READY`, `ALREADY_RUNNING`, `STOPPING`, `NOT_RUNNING`, `MODE_CONFLICT`, `CORE_NOT_READY`, `CONTOUR_DIR_BUSY`, `REVISION_CHANGED`, `INSTRUMENTS_REJECTED`, `MARKET_DATA_NOT_READY`;
- `detail` — понятная причина.

Дополнительные поля:

- `rejections` — у `INSTRUMENTS_REJECTED`: список `{instrument, reason}`, где `instrument` — имя из запроса как есть;
- `core` — у `CORE_NOT_READY`: состояние, число попыток и время следующей;
- `pid` — у `CONTOUR_DIR_BUSY`;
- `suggestions` — у `NOT_FOUND` на маршрутах `/api/v1/candles…`: подходящие короткие имена инструментов или имеющиеся таймфреймы, возможно пустой список.

Ответ НЕ ДОЛЖЕН содержать секреты. Маршруты экспорта рыночных данных и отчёта кластеров ДОЛЖНЫ сохранить прежний формат `{code, message}` в `application/json`, включая ответ `401`: их читает Historical Broker API Emulator.

#### Scenario: Отказ инструмента
- **WHEN** запуск эмуляции получил отказ по AFLT
- **THEN** ответ `422` в `application/problem+json` с `code = INSTRUMENTS_REJECTED` и `rejections = [{"instrument": "AFLT", "reason": …}]`

#### Scenario: Ошибка экспорта не меняется
- **WHEN** эмулятор данных запрашивает `GET /api/v1/market-data/changes` с курсором старше хранимой ленты
- **THEN** ответ `410` в `application/json` с `code = CURSOR_EXPIRED` и `message`, как в версии 1.8

#### Scenario: Рыночная база ещё не открыта
- **WHEN** `GET /api/v1/candles` приходит до того, как ядро открыло рыночную базу
- **THEN** ответ `503` в `application/problem+json` с `code = MARKET_DATA_NOT_READY`
