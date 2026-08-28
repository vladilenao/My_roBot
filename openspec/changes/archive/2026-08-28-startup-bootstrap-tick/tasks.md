# Tasks for startup-bootstrap-tick

## 1. Планировщик времени (timing)

- [x] 1.1 Добавить параметр `wait_boundary: bool = True` в `CandleScheduler.wait_until_bar_published` (`src/scheduler/timing.py:93`): при `False` пропускать ожидание до `next_candle_close` и сразу переходить к опросу `bar_ready` с прежним таймаутом.
- [x] 1.2 Добавить публичный метод `CandleScheduler.bar_close(bar_start)` → `bar_start + _period(bar_start)`.
- [x] 1.3 Unit-тесты в `tests/unit/scheduler/test_timing.py`: `wait_boundary=False` не спит до границы и уважает таймаут опроса; `bar_close` для `1h`, `1w` и `1M` (переменный период месяца).

## 2. Оркестратор: первый тик при запуске

- [x] 2.1 Реализовать `TradingBot._bootstrap()`: первичное наполнение кадров через `data_cache.frame_for` для всех инструментов, затем `timeline.wait_until_bar_published(self._bar_is_ready, poll_secs=TICK_POLL_SECS, timeout_secs=TICK_TIMEOUT_SECS, wait_boundary=False)`.
- [x] 2.2 Изменить `_loop()` (`src/bot/trading_bot.py:64`): на первой итерации вместо ожидания границы вызывать `_bootstrap()`; флаг `first` снимать только после успешного первого `_tick()`; исключения bootstrap обрабатываются как ошибки тика (`_report_error` + fallback-пауза).

## 3. Оркестратор: время закрытия в решении

- [x] 3.1 В `_analyze` (`src/bot/trading_bot.py:113`) заменить проставление `bar_time` на `self._timeline.bar_close(frame["datetime"].iloc[-1])`, чтобы значение стало моментом закрытия последнего бара (форматтер не меняется).

## 4. Тесты, адаптация и проверка

- [x] 4.1 Адаптировать тестовые дубли планировщика в `tests/unit/bot/test_trading_bot.py` (`FakeTimeline`, `PollingTimeline`, `TimeoutTimeline`): принять `wait_boundary`, добавить `bar_close`.
- [x] 4.2 Выровнять существующие сценарии `tests/unit/bot/test_trading_bot.py` под новую каденцию первого тика (например, счётчик heartbeat теперь включает bootstrap-тик — `test_heartbeat_every_n_ticks`).
- [x] 4.3 Новые тесты: запуск в середине периода — первый тик без ожидания границы и решение по последней закрытой свече; запуск на границе — первый тик ограниченно ждёт свежий бар (до таймаута); ошибка bootstrap доставляет «Ошибка робота».
- [x] 4.4 Прогнать полный набор тестов в venv (`python -m pytest`) и линт (`ruff check src tests`), добиться зелёного прогона.