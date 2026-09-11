## 1. Инфраструктура логирования

- [x] 1.1 Создать `src/logging_setup.py`: `setup_logging(service_uid, log_file, level)` — настройка `RotatingFileHandler` с форматом из design D1; `get_logger(name)` — возвращает `ContextLoggerAdapter`; `contextvars`: `service_uid_var`, `correlation_id_var`; `_MicrosecondFormatter` — реальные микросекунды в timestamp через `datetime.strftime()` (на macOS `time.strftime` не поддерживает `%f`, см. design D1)
- [x] 1.2 Добавить `[logging]` секцию в `default.toml` (дефолты: `service_uid`, `file`, `level`, `max_bytes`, `backup_count`)
- [x] 1.3 Добавить чтение `[logging]` секции в `src/config_loader.py` ( load_config() парсит ключи, применяет дефолты)
- [x] 1.4 Добавить `LOGGING_SERVICE_UID` в `src/config.py` (импортируемая константа, значение из loader)

## 2. Подключение в run.py

- [x] 2.1 В `run.py`: заменить `logging.basicConfig(level=logging.INFO, ...)` на `setup_logging(LOGGING_SERVICE_UID, ...)` из `src.logging_setup`
- [x] 2.2 Убедиться, что `log.info("Робот v%s запущен", __version__)` в `run.py` использует `get_logger(__name__)` (а не `logging.getLogger`)

## 3. Correlation ID в TradingBot

- [x] 3.1 В `TradingBot._tick()`: генерировать `correlation_id = f"tick-{uuid4().hex[:8]}"` в начале метода, устанавливать в `correlation_id_var`
- [x] 3.2 В `TradingBot._tick()`: сбрасывать `correlation_id_var.set(None)` после `_maybe_heartbeat()` (в finally-блоке или в конце метода)
- [x] 3.3 Убедиться, что нетиковые методы (`run()`, `_validate()`, `_bootstrap()`) корректно работают с `correlation_id = None`

## 4. Замена logging.getLogger на get_logger

- [x] 4.1 `src/bot/trading_bot.py`: `log = get_logger(__name__)` вместо `logging.getLogger(__name__)`
- [x] 4.2 `src/api/client.py`: добавить `log = get_logger(__name__)` (сейчас нет логгера)
- [x] 4.3 `src/api/instruments.py`: добавить `log = get_logger(__name__)` (если есть логирование)
- [x] 4.4 `src/data/cache.py`: `log = get_logger(__name__)`
- [x] 4.5 `src/data/loader.py`: `log = get_logger(__name__)`
- [x] 4.6 `src/scheduler/timing.py`: `log = get_logger(__name__)`
- [x] 4.7 `src/market_context/context_cache.py`: `log = get_logger(__name__)`
- [x] 4.8 `src/market_context/trend.py`: `log = get_logger(__name__)`
- [x] 4.9 `src/market_context/sr_levels.py`: `log = get_logger(__name__)`
- [x] 4.10 `src/market_structure/swings.py`: `log = get_logger(__name__)`
- [x] 4.11 `src/market_structure/harmonic.py`: `log = get_logger(__name__)`
- [x] 4.12 `src/market_structure/fibonacci.py`: `log = get_logger(__name__)`
- [x] 4.13 `src/strategies/registry.py`: `log = get_logger(__name__)`
- [x] 4.14 `src/strategies/base_strategy.py`: `log = get_logger(__name__)`
- [x] 4.15 `src/strategies/macd_rsi_stoch_strategy.py`: `log = get_logger(__name__)`
- [x] 4.16 `src/strategies/flat_triangle_strategy.py`: `log = get_logger(__name__)`
- [x] 4.17 `src/strategies/harmonic_abcd_strategy.py`: `log = get_logger(__name__)`
- [x] 4.18 `src/strategies/indicators/macd/indicator.py`: `log = get_logger(__name__)`
- [x] 4.19 `src/strategies/indicators/rsi/indicator.py`: `log = get_logger(__name__)`
- [x] 4.20 `src/strategies/indicators/stochastic/indicator.py`: `log = get_logger(__name__)`
- [x] 4.21 `src/strategies/indicators/bb/indicator.py`: `log = get_logger(__name__)`
- [x] 4.22 `src/decision/filter.py`: `log = get_logger(__name__)`
- [x] 4.23 `src/decision/risk.py`: `log = get_logger(__name__)`
- [x] 4.24 `src/execution/port.py`: `log = get_logger(__name__)`
- [x] 4.25 `src/notifier/console.py`: НЕ трогать (это UI, `print()` корректен)
- [x] 4.26 `src/notifier/telegram.py`: НЕ трогать (это UI, `print()` корректен)

## 5. Замена print() в api/retry.py

- [x] 5.1 Добавить `import logging` и `log = get_logger(__name__)` в начало `src/api/retry.py`
- [x] 5.2 Заменить строку 42 `print(f"Rate limit...")` на `log.warning("Rate limit (попытка %d/%d). Ожидание %ds, reset=%ds", attempt + 1, max_retries, delay, reset)`

## 6. Тесты

- [x] 6.1 В `tests/unit/` добавить `test_logging_setup.py`: проверка что `setup_logging()` создаёт `RotatingFileHandler`, что `get_logger()` возвращает `ContextLoggerAdapter`, что `correlation_id_var` устанавливается/сбрасывается
- [x] 6.2 Добавить тест: `notifier.notify()` НЕ проходит через логгер (проверка что консольный вывод не содержит строк логгера)
- [x] 6.3 Добавить тест: замена `print()` в `api/retry.py` на `log.warning()` (проверка что rate-limit попадает в лог)
- [x] 6.4 Прогнать полный набор тестов (`pytest tests/`) и ruff; все проверки зелёные
