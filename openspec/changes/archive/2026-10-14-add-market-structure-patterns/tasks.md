## 0. Контракты и конфигурация

- [x] 0.1 Добавить в `[market_structure]` настройки включения live-паттернов, Redis host/port/db, queue maxsize и retention; пароль оставить в окружении и валидировать параметры до старта.
- [x] 0.2 Подготовить Redis-клиент, не вызывая Redis из торгового потока.
- [x] 0.3 Добавить контрактные проверки AsyncAPI для schema version, event ID, timestamp, published_at, source, event type, direction и payload.

## 1. SQLite: контекст и outbox

- [x] 1.1 Добавить в MarketDataStore единый read-only API контекста паттерна: две последние закрытые свечи и состояния покрытия в одной read-транзакции.
- [x] 1.2 Проверить границу целевого бара и состояния received, scheduled_closed, confirmed_no_trade, pending, unresolved.
- [x] 1.3 Добавить миграцию durable outbox в market-data.sqlite3: event ID, payload, попытки, следующее время и отметка доставки.
- [x] 1.4 Покрыть единый снимок, миграцию, атомарность outbox и повтор после рестарта unit-тестами в tests/unit.

## 2. Пакет паттернов

- [x] 2.1 Создать BasePattern и PatternResult в src/market_structure/patterns/base.py.
- [x] 2.2 Реализовать bullish_engulfing.py и bearish_engulfing.py согласно spec.
- [x] 2.3 Создать закрытый реестр из двух классов в patterns/__init__.py.
- [x] 2.4 Добавить unit-тесты поглощений, равных границ, doji и недостаточного контекста в tests/unit/market_structure.

## 3. Фоновый worker

- [x] 3.1 Реализовать src/market_structure/worker.py: daemon-поток, queue, put_nowait, дедупликация и управляемая остановка.
- [x] 3.2 Добавить неблокирующую постановку копии закрытого бара в обычную и live-ветку TradingBot; исторический режим не подключать.
- [x] 3.3 Подключить согласованный контекст и инъецированный `IngestionWorker.note_loss()`: откладывать задачу и читать SQLite заново при следующей проверке покрытия.
- [x] 3.4 Реализовать точный DEBUG-лог отсутствия паттерна и warning при overflow.
- [x] 3.5 Покрыть p99 постановки не более 5 мс, overflow, повтор бара, дыру, дозаполнение, неразрешённую дыру и отсутствие паттерна.

## 4. Redis и доставка

- [x] 4.1 Реализовать запись в outbox до XADD и фоновую публикацию с повтором не ранее чем через одну секунду.
- [x] 4.2 Реализовать PatternEvent и детерминированный event ID; хранить в outbox каноническое событие без published_at, добавлять published_at из Redis TIME перед каждой попыткой XADD; Stream ID не использовать для времени рынка или дедупликации.
- [x] 4.3 Реализовать ежедневный XTRIM MINID по Redis TIME минус настроенного retention вне торгового потока.
- [x] 4.4 Добавить интеграционные тесты XADD, Consumer Group, pending 0, новый поток >, XACK, идемпотентности и retention.

## 5. Композиция и документация

- [x] 5.1 Собрать зависимости worker в run.py, включая recovery-requester live-ingestion; запускать только в live-режиме и корректно останавливать.
- [x] 5.2 Проверить, что паттерны не публикуются в EventBus и Redis-события не возвращаются в EventBus.
- [x] 5.3 Добавить документацию Redis: AOF appendfsync always, retention, Consumer Groups и pending entries.
- [x] 5.4 Зафиксировать отдельное OpenSpec-изменение как предварительное условие публикаций ClusterSource.

## 6. Приёмка

- [x] 6.1 Запустить релевантные unit-тесты.
- [x] 6.2 Валидировать openspec validate add-market-structure-patterns --strict.
- [x] 6.3 Выполнить git diff check и вручную подтвердить отсутствие сетевых вызовов Redis, брокера и recovery lane в торговом потоке.
