# Логирование
## Purpose

Structured file-based logging для отладки торгового робота. Система молча пишет ВСЮ техническую и отладочную информацию в файл `bot_debug.log`, не засоряя UI-уведомления в консоли. Идентификация сервиса (Service UID) и сквозной ID тика (Correlation ID) обеспечивают трассировку в многосервисной среде и при агрегации логов.

## Requirements

### Requirement: Service UID
Система ДОЛЖНА предоставлять фиксированный строковый UID сервиса в формате UUIDv4 (например `b7e3a1c4-92f8-4d5e-a016-7f8b2c3d4e5a`), жёстко заданный в секции `[logging]` файла `robot.toml` (или вшитый дефолт в `default.toml`). UID ДОЛЖЕН присутствовать в каждой строке лога в формате `[%(service_uid)s]`. UID ДОЛЖЕН быть доступен через `src.config.LOGGING_SERVICE_UID`.

#### Scenario: Старт сервиса
- **WHEN** робот запускается
- **THEN** `service_uid` записывается в каждую строку лога; значение не меняется при перезапусках (это UID экземпляра, а не процесса)

#### Scenario: Агрегация логов двух сервисов
- **WHEN** логи торгового робота и сервиса истории цен объединяются в один поток
- **THEN** строки разделяются по `service_uid` без анализа формата сообщения

#### Scenario: PyInstaller-бинарник
- **WHEN** робот запущен как автономный бинарник
- **THEN** `service_uid` корректно записывается в лог рядом с исполняемым файлом; PID и имя файла не используются как идентификатор

### Requirement: Correlation ID (сквозной ID тика)
Система ДОЛЖНА генерировать уникальный `correlation_id` для каждого тика обработки свечи. Формат: `tick-<8 hex chars из uuid4>` (например `tick-a3f7b2c1`). Correlation ID ДОЛЖЕН храниться в `contextvars.ContextVar` и быть доступен через `%(correlation_id)s` в шаблоне строки лога.

#### Scenario: Генерация в начале тика
- **WHEN** `TradingBot._tick()` начинает обработку новой свечи
- **THEN** `correlation_id` устанавливается в `contextvars` перед обработкой инструментов

#### Scenario: Сброс после завершения тика
- **WHEN** `TradingBot._tick()` завершает обработку (после `_maybe_heartbeat()`)
- **THEN** `correlation_id` сбрасывается в `None`; логи ожидания следующей свечи в `CandleScheduler.wait_until_bar_published()` не содержат `correlation_id` (строка `[ID:         ]`)

#### Scenario: Логи между тиками
- **WHEN** бот ожидает следующую свечу
- **THEN** строки лога содержат `[ID:         ]` (пустое значение), визуально отделяя тики друг от друга

### Requirement: Логирование исключительно в файл
Система ДОЛЖНА настроить `logging` исключительно на запись в файл `bot_debug.log` через `RotatingFileHandler`. Вывод в консоль (stderr/stdout) через логгер ДОЛЖЕН быть запрещён. `logging.basicConfig()` в `run.py` НЕ ДОЛЖЕН вызываться (или вызывается с `level=logging.CRITICAL` без хэндлеров).

#### Scenario: Чистая консоль
- **WHEN** робот работает
- **THEN** в консоли видны только уведомления `notifier.notify()` (сделки, сердцебиение, ошибки); строки логгера в консоль НЕ попадают

#### Scenario: Файл лога
- **WHEN** робот работает
- **THEN** файл `bot_debug.log` рядом с исполняемым файлом содержит все строки лога (DEBUG и выше) с шаблоном `%(asctime)s [%(levelname)s] [%(service_uid)s] [ID:%(correlation_id)s] %(name)s (%(filename)s:%(lineno)d): %(message)s`

### Requirement: Ротация лог-файлов
Система ДОЛЖНА использовать `RotatingFileHandler` с параметрами: `max_bytes=10485760` (10 MB), `backup_count=5`, `encoding="utf-8"`, `delay=True`. Файл ДОЛЖЕН называться `bot_debug.log` и располагаться рядом с исполняемым файлом.

#### Scenario: Ротация при превышении размера
- **WHEN** размер `bot_debug.log` превышает 10 MB
- **THEN** файл ротируется: старый переименовывается в `bot_debug.log.1`, создаётся новый; хранятся максимум 5 ротаций

#### Scenario: PyInstaller — файл не существует
- **WHEN** робот запускается впервые и `bot_debug.log` не существует
- **THEN** файл создаётся при первом write (параметр `delay=True`); ошибки открытия файла НЕ возникает

### Requirement: Запрет print() в горячем пути
Модуль `src/api/retry.py` НЕ ДОЛЖЕН содержать вызовы `print()`. Единственный существующий `print()` (строка 42, rate-limit) ДОЛЖЕН быть заменён на `log.warning()` с ленивым форматированием через запятую. модуль ДОЛЖЕН получить `import logging` и `log = logging.getLogger(__name__)`.

#### Scenario: Rate-limit при запросе к API
- **WHEN** Tinkoff API возвращает RESOURCE_EXHAUSTED
- **THEN** сообщение о rate-limit записывается в `bot_debug.log` через `log.warning()` с привязкой к `correlation_id` текущего тика; в консоль ничего НЕ выводится

### Requirement: Ленивое форматирование
Все строки логов ДОЛЖНЫ использовать формат через запятую (lazy formatting), а НЕ f-строки:

```python
# ПРАВИЛЬНО:
log.debug("Кадр %s: %d свечей", instrument.label, len(frame))

# НЕПРАВИЛЬНО:
log.debug(f"Кадр {instrument.label}: {len(frame)} свечей")
```

#### Scenario: Форматирование при неактивном уровне
- **WHEN** уровень лога не активен (в файловом хэндле это невозможно при DEBUG, но возможно при будущей фильтрации)
- **THEN** строка НЕ форматируется (экономия CPU)

### Requirement: Иерархия логгеров
Каждый модуль ДОЛЖЕН создавать логгер через `get_logger(__name__)` из `src/logging_setup.py`. Иерархия имён деревоится по структуре пакетов. Корневой логгер НЕ ДОЛЖЕН иметь консольных хэндлеров.

Иерархия логгеров:
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

Ключевые логгеры по архитектурным слоям:

| Слой | Корневой логгер | Уровень по умолчанию | Комментарий |
|---|---|---|---|
| Оркестратор | `src.bot` | `INFO` | Вехи тика, старт/стоп |
| API-клиент | `src.api` | `DEBUG` | Запросы к Tinkoff, ретраи, rate-limit |
| Данные | `src.data` | `DEBUG` | Загрузка свечей, кэш, инкремент |
| Рыночный контекст | `src.market_context` | `DEBUG` | Тренд, SR-уровни |
| Рыночная структура | `src.market_structure` | `DEBUG` | Свинги, Фибо, гармоника |
| Стратегии | `src.strategies` | `DEBUG` | Сигналы, индикаторы, решения |
| Решения | `src.decision` | `DEBUG` | Фильтр, риск-менеджмент |
| Исполнение | `src.execution` | `INFO` | Вызов порта (веха) |
| Планировщик | `src.scheduler` | `WARNING` | Только задержки, таймауты |
| Нотификатор | `src.notifier` | `WARNING` | Только ошибки доставки |

Фильтрация по уровням происходит **только** на уровне `FileHandler` (уровень `DEBUG`). Все логгер наследуют `NOTSET` и пропускают всё вверх до хэндлера.

#### Scenario: Имя логгера в строке
- **WHEN** модуль `src/api/retry.py` пишет лог
- **THEN** в строке лога отображается `src.api.retry` (полный путь модуля)

### Requirement: Карта логирования по уровням (Log Matrix)
Для каждого шага сценария внутри `TradingBot` система ДОЛЖНА логировать указанные данные на соответствующем уровне. Все строки логов ДОЛЖНЫ использовать ленивое форматирование через запятую (а НЕ f-строки).

#### Точка входа: `TradingBot.run()`

| Уровень | Что логируется |
|---|---|
| **INFO** | Старт робота: версия, таймфрейм, количество инструментов |
| **ERROR** | Ошибка валидации (`_validate`): текст исключения + traceback |

#### Точка: `TradingBot._tick()` — начало тика

| Уровень | Что логируется |
|---|---|
| **INFO** | Начало тика: номер тика, количество инструментов, список тикеров |
| **DEBUG** | Результат `frame_for()`: тикер, количество свечей, временной диапазон |
| **DEBUG** | Результат `refresh_if_new_candle()`: граница свечи, изменился ли кэш |
| **DEBUG** | `has_fresh_closed_bar()`: ожидаемый бар, фактический, готовность |
| **INFO** | Завершение тика: номер тика, длительность, был ли heartbeat |

#### Точка: `TradingBot._process()` — обработка инструмента

| Уровень | Что логируется |
|---|---|
| **DEBUG** | Список стратегий для инструмента |
| **INFO** | Пропуск (нет стратегий): тикер |
| **DEBUG** | Получение контекста: тикер, направление тренда, сила тренда, количество SR-уровней |
| **INFO** | Пропуск (пустой кадр): тикер |

#### Точка: `TradingBot._analyze()` — анализ по стратегии

| Уровень | Что логируется |
|---|---|
| **WARNING** | Стратегия не построена: имя стратегии, тикер |
| **DEBUG** | Результат `strategy.compute()`: имя стратегии, колонки DataFrame, количество строк |
| **DEBUG** | Результат `strategy.decide()`: имя стратегии, тип сигнала, цена, stop_loss, take_profit |
| **DEBUG** | Результат `signal_filter.apply()`: имя стратегии, сигнал до/после, направление тренда, уверенность |
| **DEBUG** | Результат `risk_manager.apply()`: имя стратегии, цена, stop_loss (уровень/fallback), take_profit (уровень/fallback) |
| **WARNING** | Ошибка стратегии (ValueError/TypeError/KeyError): имя стратегии, тикер, текст исключения |

#### Точка: `TradingBot._emit()` — исполнение

| Уровень | Что логируется |
|---|---|
| **INFO** | Решение отправлено: тикер, тип сигнала, цена, stop_loss, take_profit |

#### Точка: `TradingBot._maybe_heartbeat()` / `_send_heartbeat()`

| Уровень | Что логируется |
|---|---|
| **DEBUG** | Heartbeat: количество тиков, количество ошибок за период |
| **ERROR** | Не удалось доставить heartbeat: traceback |

#### Точка: `TradingBot._report_error()`

| Уровень | Что логируется |
|---|---|
| **ERROR** | Ошибка тика: номер тика, текст исключения + traceback |
| **ERROR** | Не удалось уведомить об ошибке: traceback |

#### Модуль: `src/api/retry.py`

| Уровень | Что логируется |
|---|---|
| **WARNING** | Rate-limit: номер попытки, максимум попыток, задержка, reset |
| **DEBUG** | Исчерпание попыток: количество попыток, текст последнего исключения |

#### Модуль: `src/data/loader.py`

| Уровень | Что логируется |
|---|---|
| **DEBUG** | Запрос свечей: тикер, тип инструмента, интервал, дата начала, дата окончания |
| **DEBUG** | Результат загрузки: количество свечей, тикер |
| **DEBUG** | Пустой результат: тикер |

#### Модуль: `src/data/cache.py`

| Уровень | Что логируется |
|---|---|
| **DEBUG** | Первичная загрузка: тикер, количество свечей |
| **DEBUG** | Инкрементальная дозагрузка: тикер, количество новых свечей |
| **DEBUG** | Состояние кэша: тикер, общее количество свечей, время последней загрузки |

#### Модуль: `src/scheduler/timing.py`

| Уровень | Что логируется |
|---|---|
| **WARNING** | Таймаут ожидания свежего бара: длительность таймаута, описание задержки публикации Tinkoff |
| **DEBUG** | Начало ожидания: целевое время свечи, задержка |
| **DEBUG** | Граничное выравнивание: таймфрейм, граница, задержка |

#### Модуль: `src/market_context/*`

| Уровень | Что логируется |
|---|---|
| **DEBUG** | TrendAnalyzer: тикер, направление тренда, сила, количество периодов |
| **DEBUG** | SRLevelsCalculator: тикер, количество support-уровней, количество resistance-уровней |
| **DEBUG** | MarketContextCache: тикер, состояние кэша, возраст контекста |

#### Модуль: `src/market_structure/*`

| Уровень | Что логируется |
|---|---|
| **DEBUG** | SwingDetector: количество обнаруженных свингов (high/low) |
| **DEBUG** | Fibonacci: координаты волны X→A, уровень ретрейсмента 61.8% |
| **DEBUG** | Harmonic: тип формации, качество |

#### Модуль: `src/strategies/*`

| Уровень | Что логируется |
|---|---|
| **DEBUG** | Регистрация стратегии: имя |
| **DEBUG** | `compute()`: имя стратегии, список колонок, количество строк |
| **DEBUG** | `decide()`: имя стратегии, тип сигнала, цена, словарь значений индикаторов |
| **DEBUG** | Индикатор MACD: fast, slow, signal, histogram |
| **DEBUG** | Индикатор RSI: значение, период |
| **DEBUG** | Индикатор Stochastic: K, D, период |
| **DEBUG** | Индикатор Bollinger Bands: upper, middle, lower, width |

#### Модуль: `src/decision/*`

| Уровень | Что логируется |
|---|---|
| **DEBUG** | SignalFilter: сигнал до/после фильтрации, направление тренда, заблокирован ли |
| **DEBUG** | RiskManager: сигнал, цена, stop_loss (уровень + метка/fallback), take_profit (уровень + метка/fallback) |

#### Модуль: `src/execution/port.py`

| Уровень | Что логируется |
|---|---|
| **INFO** | Решение доставлено: тикер, тип сигнала, цена |

### Requirement: Конфигурация в robot.toml
Секция `[logging]` файла `robot.toml` ДОЛЖНА содержать ключи: `service_uid` (строка, UUIDv4), `file` (строка, имя файла), `level` (строка, уровень DEBUG/WARNING/INFO), `max_bytes` (целое, байты), `backup_count` (целое, количество ротаций). При отсутствии секции используются вшитые дефолты.

#### Scenario: Чтение конфигурации
- **WHEN** `robot.toml` содержит секцию `[logging]`
- **THEN** параметры логирования загружаются из этой секции

#### Scenario: Отсутствие секции
- **WHEN** `robot.toml` не содержит секцию `[logging]`
- **THEN** используются вшитые дефолты: `service_uid="b7e3a1c4-92f8-4d5e-a016-7f8b2c3d4e5a"`, `file="bot_debug.log"`, `level="DEBUG"`, `max_bytes=10485760`, `backup_count=5`
