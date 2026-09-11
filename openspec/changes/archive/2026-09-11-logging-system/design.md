## Context

Робот работает в режиме «один тик = одна закрытая свеча». Текущее логирование: `logging.basicConfig(level=INFO)` в `run.py` (строка 34) выводит WARNING+ в stderr, смешиваясь с `notifier.notify()`. `api/retry.py:42` выводит rate-limit через `print()` — единственная runtime-утечка в UI. При расширении (1m timeframe, 8+ инструментов, сервис истории цен) диагностика без structured логов невозможна.

Продукт — PyInstaller one-file сборка. Лог-файл `bot_debug.log` должен быть рядом с исполняемым файлом. `notifier` (console.py / telegram.py) продолжает работать как раньше — это UI, не логирование.

### Иерархия логгеров

```
src
├── bot
│    └── trading_bot          → src.bot.trading_bot
├── api
│    ├── client               → src.api.client
│    ├── instruments          → src.api.instruments
│    └── retry                → src.api.retry
├── data
│    ├── cache                → src.data.cache
│    └── loader               → src.data.loader
├── market_context
│    ├── context_cache        → src.market_context.context_cache
│    ├── trend                → src.market_context.trend
│    ├── sr_levels            → src.market_context.sr_levels
│    └── models               → src.market_context.models
├── market_structure
│    ├── swings               → src.market_structure.swings
│    ├── harmonic             → src.market_structure.harmonic
│    └── fibonacci            → src.market_structure.fibonacci
├── strategies
│    ├── registry             → src.strategies.registry
│    ├── base_strategy        → src.strategies.base_strategy
│    ├── macd_rsi_stoch_strategy  → src.strategies.macd_rsi_stoch_strategy
│    ├── flat_triangle_strategy   → src.strategies.flat_triangle_strategy
│    ├── harmonic_abcd_strategy   → src.strategies.harmonic_abcd_strategy
│    └── indicators
│         ├── macd.indicator      → src.strategies.indicators.macd.indicator
│         ├── rsi.indicator       → src.strategies.indicators.rsi.indicator
│         ├── stochastic.indicator → src.strategies.indicators.stochastic.indicator
│         └── bb.indicator        → src.strategies.indicators.bb.indicator
├── decision
│    ├── filter               → src.decision.filter
│    └── risk                 → src.decision.risk
├── execution
│    └── port                 → src.execution.port
├── notifier
│    ├── console              → src.notifier.console
│    └── telegram             → src.notifier.telegram
├── scheduler
│    └── timing               → src.scheduler.timing
└── config                    → src.config
```

## Goals / Non-Goals

Goals:
- Выделить отдельный «невидимый» канал логирования в файл, не засоряя UI.
- Идентифицировать сервис через фиксированный `service_uid` (UUIDv4) в каждой строке лога.
- Связывать логи одного тика через `correlation_id` (сквозной ID).
- Заменить `print()` в `api/retry.py` на `log.warning()` — единственная runtime-утечка.
- Ротация файлов: 10 MB × 5 ротаций.

Non-Goals:
- Не вводить централизованное логирование (ELK, Loki) — это следующий этап.
- Не менять `notifier` — он продолжает работать через `print()` / Telegram API.
- Не добавлять HTTP-хэндлеры или network-транспорты.
- Не настраивать фильтрацию по уровням на каждом логгере — фильтрация только на FileHandler.

## Decisions

### D1. Архитектура: FileHandler + ContextLoggerAdapter
Единый `RotatingFileHandler` на корневом логгере с уровнем `DEBUG`. Все модули получают логгер через `get_logger(__name__)`, который оборачивает стандартный `logging.getLogger(__name__)` в `ContextLoggerAdapter`. Adapter при каждом вызове подставляет `service_uid` и `correlation_id` из `contextvars`.

```
┌─────────────────────────────────────────────────────┐
│  src.logging_setup                                  │
│                                                       │
│  contextvars:                                        │
│    service_uid_var  → "b7e3a1c4-..."                 │
│    correlation_id_var → "tick-a3f7b2c1" | None       │
│                                                       │
│  get_logger(__name__) → ContextLoggerAdapter          │
│    process() добавляет extra: {service_uid, corr_id} │
│                                                       │
│  setup_logging(service_uid, log_file, level):        │
│    RotatingFileHandler → bot_debug.log               │
│    Формат: %(asctime)s [%(levelname)s]               │
│             [%(service_uid)s] [ID:%(correlation_id)s]│
│             %(name)s (%(filename)s:%(lineno)d)       │
│             : %(message)s                            │
│    Время: _MicrosecondFormatter (%f → мкс)          │
└─────────────────────────────────────────────────────┘
```

**Реализация таймстампа (`_MicrosecondFormatter`).** `logging.Formatter.formatTime()` по умолчанию использует `time.strftime()`, а на macOS `%f` в нём не поддерживается и печатается литеральной буквой `f` (`11:20:17.f` вместо `11:20:17.093719`). Поэтому таймстамп выводит кастомный подкласс `_MicrosecondFormatter` (наследует `logging.Formatter`, переопределяет `formatTime()` через `datetime.strftime()`). Видимый формат времени остаётся прежним — `%Y-%m-%d %H:%M:%S.%f` с реальными микросекундами на всех платформах.

### D2. Correlation ID: contextvars в _tick()
`correlation_id` генерируется в `TradingBot._tick()` перед обработкой инструментов и сбрасывается в `None` после `_maybe_heartbeat()`. Формат `tick-<8hex>` — достаточно уникальности при масштабе одиночного бота (~4 млрд комбинаций). Все нетиковые методы (`run()`, `_validate()`, `_bootstrap()`, `CandleScheduler.wait_until_bar_published()`) работают с `correlation_id = None`.

Почему сбрасывать: логи ожидания следующей свечи не должны склеиваться с предыдущим тиком. Строка `[ID:         ]` (пустое значение) визуально отделяет тики.

### D3. Запрет вывода в консоль
`logging.basicConfig()` в `run.py` **не вызывается**. Создаётся только `FileHandler`. `notifier.notify()` по-прежнему пишет в консоль через `print()` (console.py) или Telegram API (telegram.py) — это UI, он не проходит через логгер.

### D4. Замена print() в api/retry.py
Строка 42 `print(f"Rate limit (попытка {attempt + 1}/{max_retries}). Ожидание {delay}с...")` заменяется на `log.warning("Rate limit (попытка %d/%d). Ожидание %ds, reset=%ds", attempt + 1, max_retries, delay, reset)`. Дополнительно добавляется `import logging` и `log = logging.getLogger(__name__)` в начало файла. Это единственный `print()` в горячем пути бота.

### D5. Конфигурация: секция [logging] в robot.toml
```toml
[logging]
service_uid = "b7e3a1c4-92f8-4d5e-a016-7f8b2c3d4e5a"
file = "bot_debug.log"
level = "DEBUG"
max_bytes = 10485760       # 10 MB
backup_count = 5
```

Значения по умолчанию в `default.toml`. `LOGGING_SERVICE_UID` добавляется в `src/config.py` как импортируемая константа.

### D6. Ленивое форматирование
Все строки логов строго через запятую: `log.debug("Кадр %s: %d свечей", label, count)` — не f-строки. Форматирование происходит только при реальной записи.

## Log Matrix: что логировать на каждом уровне

### DEBUG — сырые данные для отладки

| Модуль | Что именно логировать |
|---|---|
| `src/data/loader.py` | Ответы Tinkoff API: тикер, тип, интервал, диапазон дат, количество загруженных свечей |
| `src/data/cache.py` | Массивы свечей из кэша: тикер, размер фрейма, временной диапазон, результат инкрементальной дозагрузки |
| `src/strategies/indicators/macd` | Значения индикаторов: fast, slow, signal, histogram |
| `src/strategies/indicators/rsi` | Значения индикаторов: value, period |
| `src/strategies/indicators/stochastic` | Значения индикаторов: K, D, period |
| `src/strategies/indicators/bb` | Значения индикаторов: upper, middle, lower, width |
| `src/market_structure/fibonacci` | Уровни Фибоначчи: координаты волны X→A, ratios ретрейсмента и расширения |
| `src/market_structure/swings` | Свинги: количество обнаруженных вершин и впадин, их индексы и цены |
| `src/market_structure/harmonic` | Гармонические формации: тип, точки, метрика качества |
| `src/strategies/*` | `compute()`: колонки DataFrame, количество строк; `decide()`: тип сигнала, цена, словарь indicator_values |
| `src/decision/*` | `SignalFilter`: сигнал до/после, направление тренда, заблокирован; `RiskManager`: SL/TP (уровень/fallback), метки уровней |

### INFO — вехи оркестратора

| Модуль | Что именно логировать |
|---|---|
| `src/bot/trading_bot` | Старт робота, начало тика (номер, инструменты), завершение тика (номер, длительность) |
| `src/bot/trading_bot` | Пропуск инструмента (нет стратегий / пустой кадр) |
| `src/bot/trading_bot` | Решение отправлено через `execution.execute()` |
| `src/execution/port.py` | Доставка решения: тикер, тип сигнала, цена |

### WARNING — задержки и rate-limit

| Модуль | Что именно логировать |
|---|---|
| `src/api/retry.py` | Срабатывание rate-limit: номер попытки, максимум, задержка, reset |
| `src/scheduler/timing.py` | Задержки в ожидании свежего бара: длительность таймаута, описание причины (публикация Tinkoff) |
| `src/bot/trading_bot` | Стратегия не построена: имя стратегии, тикер |
| `src/bot/trading_bot` | Ошибка стратегии (ValueError/TypeError/KeyError): имя, тикер, исключение |

### ERROR/CRITICAL — падения и критические сбои

| Модуль | Что именно логировать |
|---|---|
| `src/bot/trading_bot` | Необработанное исключение тика: traceback |
| `src/bot/trading_bot` | Ошибка валидации привязок: текст исключения |
| `src/bot/trading_bot` | Не удалось доставить heartbeat / уведомление об ошибке: traceback |
| `src/api/retry.py` | Исчерпание попыток API (после max_retries): текст последнего исключения |
| `src/data/loader.py` | Падение потока свечей API: неожиданный ответ / нарушение формата данных |
| `src/config.py` | Ошибки конфигурации торговых пар: неизвестное имя стратегии, невалидный таймфрейм |

## Risks / Trade-offs

- **print() в api/retry.py** — критический предшественник: замена на `log.warning()` требуется ДО внедрения остального. Без этого rate-limit остаётся вне системы логирования.
- **PyInstaller и файл лога** — `RotatingFileHandler(delay=True)` решает проблему: файл не открывается до первого write. При старте файла может не быть — это нормально.
- **contextvars и многопоточность** — текущий бот однопоточный, но будущий сервис истории цен может быть многопоточным. `contextvars` потокобезопасны.
- **Рост файла** — 10 MB × 5 ротаций = максимум ~50 MB на диске. При 1h timeframe и DEBUG-уровне — ~30 дней логов.

## Migration Plan

1. Создать `src/logging_setup.py` с `setup_logging()`, `get_logger()`, `contextvars`.
2. Добавить `[logging]` секцию в `default.toml` и `robot.toml`.
3. Добавить `LOGGING_SERVICE_UID` в `src/config.py`.
4. В `run.py`: заменить `logging.basicConfig()` на `setup_logging()`.
5. Заменить `log = logging.getLogger(__name__)` на `log = get_logger(__name__)` во всех модулях.
6. Установить `correlation_id` в `TradingBot._tick()` и сбросить после `_maybe_heartbeat()`.
7. Заменить `print(...)` в `src/api/retry.py:42` на `log.warning(...)`.
8. Прогнать тесты (`pytest tests/`), ruff.
9. Проверить ротацию: создать файл > 10 MB, убедиться что создаётся `.1`, `.2` и т.д.

## Open Questions

- Нужно ли добавить unit-тест на проверку что `notifier.notify()` не проходит через логгер? (Да, это критично для гарантии чистоты UI.)
- Стоит ли вынести `print()` в `notifier/telegram.py:22,32,34` (fallback при ошибке Telegram) в логгер? (Нет — это UI, пользователь должен видеть ошибку доставки.)
