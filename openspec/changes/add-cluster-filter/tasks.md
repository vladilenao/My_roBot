## 1. Конфигурация

- [x] 1.1 `Assignment.clusters` (`off`/`shadow`/`enforce`, по умолчанию `off`) в `src/strategies/contracts.py`; ключ `clusters` в `_STRATEGY_ENTRY_KEYS` и его проверка в `src/config_loader.py`.
- [x] 1.2 Секция `[clusters]` со строгими ключами и ограничениями, константы в `src/config.py`; закомментированный пример в `default.toml`.
- [x] 1.3 Unit-тесты конфигурации: дефолты, привязка без ключа, неверный режим, незнакомый ключ, окна.

## 2. Хранилище и чтение потока

- [x] 2.1 `src/clusters/store.py`: `cluster-events.sqlite3` (WAL, `user_version = 1`), запись пачки `INSERT OR IGNORE`, выборки по `secid`/`published_at`, по `signal_id`, по семейству; `consumer_state` (D1).
- [x] 2.2 `src/clusters/consumer.py`: `ClusterEventsConsumer` — создание группы, pending → новые, запись → `XACK`, повтор с паузой до 60 с, `last_read_at` (D1).
- [x] 2.3 Догрузка потока `XRANGE` перед историческим прогоном (D2).
- [x] 2.4 Unit-тесты на фейковом Redis: подтверждение после записи, повтор `event_id`, ошибка записи, недоступный Redis, отбор `signal_confirmed`.

## 3. Вердикт

- [x] 3.1 `src/clusters/verdict.py`: `ClusterVerdict` и `evaluate(...)` по D5 — σ15, ход, диапазон, метки событий, правило; чистая функция от свечей, событий, порогов и времени.
- [x] 3.2 Сопоставление инструмента и `contract_mismatch` (D3); источник 1m свечей через провайдер кадров (D4).
- [x] 3.3 Текст `message` (D7): короткие имена, время МСК, событие-улика.
- [x] 3.4 Unit-тесты: пример BR-11.26 15.10 из D5 с точными числами, `veto_strong`, `confirm`, `neutral` при ходе < порога, каждая причина `unknown`, событие после `T`, отзыв до и после `T`.

## 4. Допуск

- [x] 4.1 `TradeManager._plan_entry`: вердикт до фильтра профиля, трасса `filter.cluster` (D8), `SignalAdmission.cluster`; в `enforce` — недопуск `cluster-climax` после фильтра (D6); текст кода в `_REJECTION_MESSAGES`.
- [x] 4.2 Композиция в `run.py`: хранилище, потребитель (live), догрузка (история), провайдер свечей; остановка потребителя при выходе.
- [x] 4.3 Unit-тесты допуска: shadow не меняет допуск, enforce отклоняет, `enforce_level`, `unknown` в enforce пропускает, отказ `basic_levels` с вердиктом, `_manage_owned` без вердикта.

## 5. События и консоль

- [x] 5.1 Необязательный ключ `clusters` у событий `signal` и `rejected` в `src/events/schema.py`; передача из `TradingBot` и `LiveAnalysis`.
- [x] 5.2 `console_view._plan` и `_rejected`: строка «Кластеры» (D7); `neutral` без строки.
- [x] 5.3 Тесты консоли: теневой запрет под ПЛАН, `neutral`, `unknown` без биржевого кода, боевой запрет, отказ фильтра тренда с вердиктом.

## 6. Отчёт

- [x] 6.1 `src/clusters/report.py`: сбор отчёта по D9 из `calculations`, сделок и 1m свечей.
- [x] 6.2 Маршрут `GET /api/v1/clusters/report` в `src/data/export_api.py`: параметры, 400/401/503, `application/hal+json`, ссылки.
- [x] 6.3 Тесты: отчёт за день по примеру BR-11.26, период и ссылки, `pending`, пустой период, неверные параметры, без токена; ответ проверяется по схеме `ClustersReport` из `docs/openapi.yaml`.

## 7. Документация и завершение

- [x] 7.1 Единый `docs/openapi.yaml` с тегом `Clusters` и маршрутом отчёта; `export_api._OPENAPI` указывает на него (D10).
- [x] 7.2 `docs/trade-management/signal-pipeline.md` — шаг «Кластеры» в допуске; `docs/database.md` — `cluster-events.sqlite3`; `ARCHITECTURE.md` — пакет `src/clusters/`.
- [x] 7.3 Запись в `CHANGELOG.md`.
- [x] 7.4 Полный `pytest`, Ruff, `openspec validate add-cluster-filter --strict`.
