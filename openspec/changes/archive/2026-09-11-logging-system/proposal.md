## Why

Текущая система логирования `My_roBot` не решает задачу отладки: `logging.basicConfig(level=INFO)` в `run.py` выводит WARNING+ в stderr, смешиваясь с UI-уведомлениями `notifier.notify()`. Rate-limit в `api/retry.py:42` выводится через `print()` — техническая информация засоряет консоль и не попадает в лог-файл. При расширении (таймфрейм 1m, 8+ инструментов, параллельный сервис истории цен) диагностика без structured логов невозможна.

Цель: выделить отдельный «невидимый» канал логирования, который молча пишет ВСЮ техническую информацию в файл `bot_debug.log`, не засоряя UI в консоли. Пользователь по-прежнему видит только бизнес-события (сделки, сердцебиение, ошибки) через `notifier`.

## What Changes

- **Создаётся `src/logging_setup.py`**: модуль настройки логирования с `RotatingFileHandler`, `ContextLoggerAdapter` и `contextvars` для `service_uid` / `correlation_id`.
- **Новая секция `[logging]` в `robot.toml` / `default.toml`**: `service_uid` (фиксированный UUIDv4), `file`, `level`, `max_bytes`, `backup_count`.
- **`correlation_id` в `TradingBot._tick()`**: генерация `tick-<8hex>` в начале тика, сброс в `None` после `_maybe_heartbeat()`. Все логи тика связаны сквозным ID.
- **Запрет вывода в консоль**: `logging.basicConfig()` отключается, создаётся только `FileHandler`. `notifier.notify()` по-прежнему пишет в консоль напрямую (не через логгер).
- **Замена `print()` в `api/retry.py:42`** на `log.warning()` — единственный runtime `print()`, мешающий чистоте логов.

## Capabilities

### New Capabilities
- `logging`: structured file-based logging для отладки — service UID, correlation ID, ротация файлов, ленивое форматирование.

### Modified Capabilities
- (нет изменений существующих модулей — notifier продолжает работать как раньше)

## Impact

- Создаются: `src/logging_setup.py`, `[logging]` секция в `default.toml` / `robot.toml`.
- Правятся: `run.py` (вызов `setup_logging()` вместо `basicConfig()`), `src/config.py` (добавление `LOGGING_SERVICE_UID`), `src/api/retry.py` (замена `print()` на `log.warning()`), все модули (замена `logging.getLogger()` на `get_logger()`).
- Тесты: проверка что `notifier.notify()` не проходит через логгер, проверка ротации, проверка привязки `correlation_id`.
- Внешние зависимости: только stdlib (`logging`, `contextvars`).
