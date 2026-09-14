## 1. Конфигурация и расширение лестницы таймфреймов

- [x] 1.1 Добавить `"30m": CandleInterval.CANDLE_INTERVAL_30_MIN` и `"4h": CandleInterval.CANDLE_INTERVAL_4_HOUR` в словарь `TIMEFRAMES` в `src/config.py`
- [x] 1.2 Добавить `"30m": ("minute", 30)` и `"4h": ("hour", 4)` в `_PERIODS` в `src/scheduler/timing.py`
- [x] 1.3 Реализовать чистую функцию `tf_period_minutes(timeframe: str) -> int` в `src/scheduler/timing.py` (возвращает длительность ТФ в минутах: для 1M ≈ 43200)
- [x] 1.4 Создать `src/decision/filters/triple_screen.py`: frozen dataclass `TripleScreenParams` с полями `multiplier: int = 5`, `macd_fast/slow/signal`, `stoch_k/d/smooth_k`, `oversold/overbought` и методом `from_config(section: dict) -> TripleScreenParams`, выполняющим валидацию (неизвестные ключи → ValueError)
- [x] 1.5 Реализовать чистую функцию `tf_hierarchy(work_tf: str, multiplier: int, ladder: Sequence[str]) -> tuple[str, str]` в `src/decision/filters/triple_screen.py` (возвращает `(tf_int, tf_htf)` на основе `tf_period_minutes`; если лестница не содержит подходящего ТФ → `ValueError`)
- [x] 1.6 В `src/config_loader.py` добавить чтение секции `[strategies.filter.triple_screen]` (ключи: `multiplier`, `macd_fast/slow/signal`, `stoch_k/d/smooth_k`, `oversold/overbought`); неизвестные ключи → `ConfigError` (режим строгой валидации `_SECTIONS`)
- [x] 1.7 В `src/config_loader.py` добавить валидацию иерархии для каждой привязки с `filter="triple_screen"`: вызов `tf_hierarchy(assignment.tf, params.multiplier, TIMEFRAMES.keys())`; при `ValueError` → `ConfigError` с указанием привязки
- [x] 1.8 Добавить дефолты в `default.toml`: `TIMEFRAMES` включает `30m`/`4h`; `ACTIVE_TIMEFRAMES` остаётся `{5m,15m,1h}`; секция `[strategies.filter.triple_screen]` с дефолтами Элдера
- [x] 1.9 Написать unit-тесты для `tf_hierarchy`: M5→M30→H4 (дефолт), M15→H1→H4 (multiplier=3), M1 → ValueError; проверка вычислений через `tf_period_minutes`
- [x] 1.10 Написать unit-тесты для config_loader: отсутствие секции → дефолты; неизвестный ключ секции → ConfigError; привязка с невалидным TF (например `1M`) → ConfigError

## 2. Слой данных: загрузка кадров старших ТФ

- [x] 2.1 Добавить публичный метод `ensure_loaded(instrument, timeframe: str)` в `src/data/cache.py` (если key отсутствует → вызвать `_initial_load`; если key есть → выполнить инкрементальную дозагрузку по аналогии с `refresh_if_new_candle`, используя текущий boundary как `max_loaded`)
- [x] 2.2 Создать `src/data/htf_provider.py` с классом `HtfFrameProvider(cache, timeline)` и методом `frame_for(instrument, timeframe: str, *, max_close: datetime, min_bars: int) -> pd.DataFrame`: (a) `ensure_loaded`, (b) `_closed_only` (frame[frame.datetime < boundary]), (c) alignment `bar_close(grid.bar_close(datetime)) <= max_close`, (d) `len(frame) >= min_bars` guard (warning in log if not)
- [x] 2.3 Написать unit-тесты для `MarketDataCache.ensure_loaded`: уже загружен → вызов `_load` с `start_date=last_dt`; не загружен → полный `_initial_load`
- [x] 2.4 Написать unit-тесты для `HtfFrameProvider`: свечи с `bar_close > max_close` отбрасываются; `min_bars` deficiency → DataFrame короче, warning в логе; `ensure_loaded` вызывается

## 3. Реализация TripleScreenFilter

- [x] 3.1 В `src/decision/filters/triple_screen.py`: frozen dataclass `TripleScreenFilter(provider, params: TripleScreenParams)` с методом `apply(decision, ctx, instrument="", timeframe="") -> Decision` (параметр `data_provider` переименован в `provider` по решению design:5)
- [x] 3.2 Реализовать экран 1 (направление на `TF_htf`): получить кадр через `provider.frame_for(...)` с `max_close=bar_close_work` и `min_bars=macd.warmup+2`; вычислить `MacdIndicator(...).compute(frame)`; сравнить `hist[-1]` vs `hist[-2]`; вернуть `allowed_direction: BUY | SELL | None`
- [x] 3.3 Реализовать экран 2 (коррекция на `TF_int`): получить кадр с `min_bars=stoch.warmup+1`; вычислить `StochasticIndicator(...).compute(frame)`; `%K[t] < oversold` → BUY confirmation; `%K[t] > overbought` → SELL confirmation; иначе `None`
- [x] 3.4 Реализовать экран 3 (вход): `SignalType.HOLD` → вернуть без изменений; `BUY` → вернуть BUY только если `screen1==BUY` и `screen2==BUY`, иначе HOLD; `SELL` → аналогично; если любой из кадров `frame_for` вернул DataFrame длины < required_warmup → HOLD + warning в `bot_debug.log`
- [x] 3.5 В `src/decision/filters/__init__.py`: добавить запись `"triple_screen": None` в `PROFILES` (placeholder); реальный экземпляр создаётся в `run.py`

## 4. Связывание: фасад, trading_bot, run.py

- [x] 4.1 В `src/decision/filters/base.py`: расширить сигнатуру `ProfileFilter.apply(decision, ctx, instrument="", timeframe="") -> Decision`
- [x] 4.2 В `src/decision/filter.py`: расширить `SignalFilter.apply(..., instrument="", timeframe="")`, передавая в `profile.apply(decision, ctx, instrument=instrument, timeframe=timeframe)`
- [x] 4.3 В `src/bot/trading_bot.py` (`_analyze`, ~строка 195): передать `instrument=instrument, timeframe=tf` в `self._signal_filter.apply(...)` (передаётся объект `Instrument`, а не `instrument.label` — label непригоден для поиска в `MarketDataCache._key`; уточнено в design:4)
- [x] 4.4 В `run.py`: после инициализации `data_cache` и `timeline` — прочитать параметры triple_screen из конфига, создать `HtfFrameProvider(data_cache, timeline)`, создать `TripleScreenFilter(provider=htf_provider, params=params)`, зарегистрировать `PROFILES["triple_screen"] = triple_screen_filter`
- [x] 4.5 Проверить существующие unit-тесты фильтра (`tests/unit/test_trading_bot.py`, `tests/unit/test_notifier/`); убедиться, что дефолтные `instrument=""`, `timeframe=""` не ломают существующие assertions

## 5. Тесты

- [x] 5.1 Запустить полный набор pytest (≥476 тестов) и убедиться в отсутствии регрессий
- [x] 5.2 Написать unit-тесты `TripleScreenFilter` с mock-провайдером: (a) BUY проходит; (b) BUY блокируется экраном 1; (c) BUY блокируется экраном 2; (d) HOLD pass-through; (e) HOLD при нехватке баров; (f) проверка `bar_close` alignment
- [x] 5.3 Написать unit-тесты `HtfFrameProvider.frame_for` с реальным mock-cache и mock-timeline: alignment, min_bars, ensure_loaded
- [x] 5.4 Написать integration-тест `tf_hierarchy` в контексте config_loader: привязка `macd_rsi_stoch` с `filter="triple_screen"` + `tf="5m"` проходит; `tf="1M"` → `ConfigError`

## 6. Финальная проверка и сборка

- [x] 6.1 Запустить линтер (`ruff check src tests`) и исправить предупреждения
- [x] 6.2 Убедиться, что все ≥476 тестов проходят
- [x] 6.3 Убедиться, что существующие тесты фильтра и notifier не сломаны расширением сигнатуры `apply()`
- [ ] 6.4 Зафиксировать изменения: `git commit -m "feat(filter): Triple Screen filter by Elder methodology"`