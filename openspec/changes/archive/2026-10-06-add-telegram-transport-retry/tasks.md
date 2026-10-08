## 1. Конфигурация max_transport_attempts

- [x] 1.1 Добавить ключ `notifier_telegram_request_timeout`-стиля: `max_transport_attempts` в `_EXPECTED_TYPES`, `_parse_notifier_subsection` (только `[notifier.telegram]`, целое ≥ 1), `_validate` (bool и значения < 1 — ConfigError)
- [x] 1.2 Добавить дефолт `notifier_telegram_max_transport_attempts: 1` в `_DEFAULTS` (`src/config.py`) и экспорт `NOTIFIER_TELEGRAM_MAX_TRANSPORT_ATTEMPTS`
- [x] 1.3 Прописать `max_transport_attempts = 2` в `default.toml` в `[notifier.telegram]` с комментарием
- [x] 1.4 Пробросить значение через `src/notifier/factory.py` в `TelegramChannel`

## 2. Транспорт: признак retryable

- [x] 2.1 `src/notifier/telegram_transport.py`: поле `retryable` у `ApiResult` (конструктор/фабричные методы), `True` только для транспортных исключений, `False` для разобранных ответов
- [x] 2.2 `src/notifier/telegram.py`: считать число попыток по строкам `attempts` ключа доставки; не фиксировать промежуточный `uncertain`, пока разрешён повтор

## 3. Повтор в _deliver

- [x] 3.1 Один повтор того же `request(...)` при `retryable` и отсутствии подтверждённого `message_id`, с паузой ~2 с; после подтверждения — сохранить `message_id` и прекратить повторы
- [x] 3.2 Финальный статус последней попытки писать в БД как раньше (undelivered → `uncertain`/`failed` по текущим правилам)

## 4. Тесты

- [x] 4.1 `tests/unit/notifier/test_visual_delivery.py`: обновить сценарий «повтора нет» (97, 186) под новую семантику; новый тест: uncertain + retryable → ровно один повтор, `message_id` сохраняется
- [x] 4.2 Тест: `ok=false`/429 — повторов нет; вторая неопределённость — повторов больше нет
- [x] 4.3 Тест конфиг-лоадера: `max_transport_attempts` парсится, в console-подсекции — ConfigError, значение 0 — ConfigError; тест фабрики: значение доходит до канала
- [x] 4.4 Тест transportа: `ApiResult` помечен `retryable` только при исключении

## 5. Проверки

- [x] 5.1 `.venv/bin/python -m pytest -q` — зелёный
- [x] 5.2 `.venv/bin/python -m ruff check .` — без новых ошибок (tools/ и openspec/_check_sync.py допустимы)
- [x] 5.3 `openspec validate --change add-telegram-transport-retry` — без ошибок
