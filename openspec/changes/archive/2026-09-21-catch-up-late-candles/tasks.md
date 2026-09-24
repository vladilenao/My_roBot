## 1. Конфигурация нового ключа

- [x] 1.1 `src/config_loader.py`: добавить `"catch_up_bars": "tick_catch_up_bars"` в `_SECTIONS["tick"]` и `"tick_catch_up_bars": int` в `_EXPECTED_TYPES`
- [x] 1.2 `src/config.py`: добавить `"tick_catch_up_bars": 2` в `_DEFAULTS` и константу `CATCH_UP_BARS = _CONFIG["tick_catch_up_bars"]`
- [x] 1.3 Unit-тест конфига: ключ опционален (старый robot.toml без ключа валиден), недопустимый тип → `ConfigError`

## 2. Планировщик: догон поздно опубликованных баров (D1)

- [x] 2.1 `src/scheduler/timing.py`: параметр `catch_up_bars: int = 0` у `MultiTimeframeScheduler`; состояние `_pending: dict[str, datetime]` (ТФ → ждущая граница); метод-хелпер периода ТФ через `grid(tf)` (как в `fallback_secs`)
- [x] 2.2 Кандидаты `wait_until_bar_published` = `_crossed_since(_last_tick)` ∪ pending в пределах горизонта `catch_up_bars × period` (для `wait_boundary=False` и для штатного тика); после окна `_pending = candidates - ready`, сбросив вышедшие за горизонт с логом «бар ТФ X потерян: горизонт догона истёк»
- [x] 2.3 Unit-тесты timing: бар, пришедший после истечения окна, попадает в `ready` на следующем тике; догон держится несколько тиков в пределах горизонта; за горизонтом ТФ выпадает из pending и тики продолжаются без него; `catch_up_bars=0` даёт прежнее поведение; bootstrap-путь (`wait_boundary=False`)

## 3. Кэш: терпимая готовность таймфрейма (D2)

- [x] 3.1 `src/data/cache.py`: параметр `freshness_tolerance_bars: int = 0` в конструкторе `MarketDataCache`; загрузка периода ТФ через `self._timeline.grid(tf)` (единый источник с D1)
- [x] 3.2 `has_fresh_closed_bar`: платить что «неактивные» пары (последний бар `>= expected - tol`, иначе старше) исключаются из гейта; нет активных пар → `False` (не выдумываем свежесть); иначе `True`, если у каждой активной пары бар `>= expected`; пустой набор кадров ТФ → `True` (как сейчас); DEBUG-лог исключённых пар
- [x] 3.3 Unit-тесты кэша: отстающая >tol пара не блокирует гейт; все пары свежие → `True`; свежих ни у кого нет → `False`; нет кадров → `True`; `freshness_tolerance_bars=0` = прежний жёсткий AND

## 4. Плавная стартовая загрузка (D4)

- [x] 4.1 `src/data/cache.py`: `_initial_load` перед обращением к API ждёт остаток `data_refresh_min_interval` с последнего вызова (`_wait_throttle()` через `time.sleep`); `_load` по-прежнему обновляет `_last_api_attempt`
- [x] 4.2 `src/instruments/selector.py`: `select_instruments(validation_pause_secs=0)` и `_validate_instruments(..., pause_secs)` — пауза между `find_working_instrument`; при исключении с `RESOURCE_EXHAUSTED` — ожидание `rate_limit_reset_secs(exc)` и продолжение (не прерывать выбор)
- [x] 4.3 Unit-тесты: `_initial_load` спит недостающую часть интервала (подмена `time.sleep`); селектор соблюдает паузу между валидациями; при rate-limit ждёт `ratelimit_reset` и продолжает

## 5. Сборка в run.py

- [x] 5.1 `run.py`: `MultiTimeframeScheduler(..., catch_up_bars=CATCH_UP_BARS)`; `MarketDataCache(..., freshness_tolerance_bars=CATCH_UP_BARS)`; `select_instruments(validation_pause_secs=DATA_REFRESH_MIN_INTERVAL)`
- [x] 5.2 Проверить, что `default.toml`/`robot.toml` трогать не нужно (ключ опционален), `_SECTIONS`/`_EXPECTED_TYPES` не конфликтуют с секцией `[tick]`

## 6. Верификация

- [x] 6.1 `.venv/bin/python -m pytest -q` (новые unit-тесты + регресс)
- [x] 6.2 `.venv/bin/python -m ruff check .` (подтвердить отсутствие новых ошибок; прежние в `tools/` и `openspec/_check_sync.py` терпимы)