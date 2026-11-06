## MODIFIED Requirements

### Requirement: Ошибки API управления
Нормативный уровень: MUST. Ответ с ошибкой на точке входа и на маршрутах управления (`/api/v1/core`, `/api/v1/emulation…`, `/api/v1/sandbox…`, `/api/v1/history…`, `/api/v1/candles…`) ДОЛЖЕН быть `application/problem+json` (RFC 9457) с полями:

- `type` — `urn:my-robot:problem:<код>`, код в нижнем регистре через дефис;
- `title` — короткое название ошибки;
- `status` — код HTTP;
- `code` — код ошибки: `INVALID_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN_HOST`, `NOT_FOUND`, `REPORT_NOT_READY`, `ALREADY_RUNNING`, `STOPPING`, `NOT_RUNNING`, `MODE_CONFLICT`, `CORE_NOT_READY`, `CONTOUR_DIR_BUSY`, `REVISION_CHANGED`, `INSTRUMENTS_REJECTED`, `MARKET_DATA_NOT_READY`, `JOURNAL_UNAVAILABLE`;
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

#### Scenario: Журнал контура не читается
- **GIVEN** `trades.sqlite3` эмуляции повреждён
- **WHEN** приходит `GET /api/v1/emulation/trades`
- **THEN** ответ `503` в `application/problem+json` с `code = JOURNAL_UNAVAILABLE`, в `detail` нет пути к файлу и текста исключения
