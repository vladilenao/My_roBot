## MODIFIED Requirements

### Requirement: Публикация паттерна и схема события

Нормативный уровень: MUST.
Воркер `market_structure` MUST быть единственным издателем паттернов в Redis Stream `market.events` через `XADD`. `market.events` MUST содержать только неизменяемые факты о рынке; команды роботу и управляющие сообщения в него публиковать запрещено. Каждое сообщение MUST соответствовать контракту `PatternEvent v1` ниже: обязательные `schema_version=1`, `event_id`, `ticker`, `timeframe`, `timestamp` в UTC ISO 8601 (время закрытия), `published_at` в UTC ISO 8601, `source='candle_patterns'`, `event_type`, `direction` и `payload`. `source` обозначает модуль-издатель, `event_type` — вид факта. `event_id` MUST детерминированно идентифицировать `(source, schema_version, ticker, timeframe, timestamp, event_type)` для устранения повторной обработки. `timestamp` задаёт предметное время события. `published_at` MUST браться из `Redis TIME` непосредственно перед попыткой `XADD` и входить только в успешно записанное сообщение; при повторной попытке его значение может измениться, а `event_id` остаётся постоянным. Redis Stream ID не является временем рынка. Payload MUST включать `body_overlap_ratio` и OHLC обеих свечей. До `XADD` в устойчивый outbox существующей SQLite БД `market-data.sqlite3` MUST сохраняться каноническая часть события без `published_at` с уникальным `event_id`; после успешного ответа `XADD` outbox-запись MUST помечаться доставленной. При рестарте воркер MUST повторять недоставленные записи с тем же `event_id`. После ошибки Redis outbox-запись MUST получить следующую попытку не ранее чем через одну секунду. Сетевые задержки, повтор и `XADD` MUST оставаться в фоне. Redis MUST использовать AOF `appendfsync always`; подтверждение публикации возможно только после успешного ответа `XADD`.

#### Scenario: Паттерн опубликован
- **GIVEN** `bullish_engulfing` вернул валидный `PatternResult`
- **WHEN** воркер сериализует результат и Redis подтверждает `XADD`
- **THEN** `market.events` содержит сообщение с `schema_version=1`, `event_type=bullish_engulfing`, `direction=long`, предметным `timestamp`, временем записи `published_at`, валидным `payload` и постоянным `event_id`

#### Scenario: Redis временно недоступен
- **GIVEN** детектор нашёл паттерн, а Redis не отвечает
- **WHEN** фоновая публикация получает ошибку
- **THEN** воркер сохраняет задачу публикации для повторной попытки, не вызывает Redis из основного потока и не объявляет событие доставленным

#### Контракт: PatternEvent v1

Поля Redis Stream являются строками; `payload` передаётся JSON-строкой и после декодирования является объектом. `payload` обязательно содержит число `body_overlap_ratio` в диапазоне `[0, 1]`, а также объекты `previous_candle` и `current_candle` с числовыми `open`, `high`, `low`, `close`. Дополнительные поля внутри `payload` разрешены. Допустимы только пары `event_type=bullish_engulfing` с `direction=long` и `event_type=bearish_engulfing` с `direction=short`. Полная машиночитаемая AsyncAPI-схема хранится в `docs/api/market-events.asyncapi.yaml`.
