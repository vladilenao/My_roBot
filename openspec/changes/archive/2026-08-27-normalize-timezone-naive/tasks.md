## 1. Общий helper нормализации времени

- [x] 1.1 Создать `src/data/timeutil.py` с `to_naive(dt)` (приводит `datetime`/`pd.Timestamp` к tz-naive через `tz_localize(None)` при наличии `tzinfo`)
- [x] 1.2 Добавить unit-тест `tests/unit/data/test_timeutil.py`: naive без пояса остаётся naive, aware с пояса приводится к naive без ошибки

## 2. Починка падения в loader (приоритет)

- [x] 2.1 В `src/data/loader.py` привести `start_date` и `end_date` к единому виду до сравнения `start_date >= end_date` (устраняет `can't compare offset-naive and offset-aware datetimes`)
- [x] 2.2 Убедиться, что `from_`/`to`, передаваемые в API, — `aware UTC` (клиент Tinkoff не принимает naive; сводка через `to_aware_utc`)
- [x] 2.3 Обновить/добавить unit-тесты `tests/unit/data/test_loader.py`: смешанные naive/aware границы сравниваются без ошибки, результат корректен

## 3. Планировщик (timing) → naive

- [x] 3.1 В `src/scheduler/timing.py` `clock` возвращает naive (убрать `datetime.now(timezone.utc)` → `datetime.now()`); убрать `astimezone(timezone.utc)` в `current_candle_start`/`next_candle_close`
- [x] 3.2 Убрать `tzinfo=timezone.utc` в `_month_span` (наивные конструкторы)
- [x] 3.3 Обновить unit-тесты `tests/unit/scheduler/test_timing.py`: моменты naive (без пояса), границы таймфреймов и `1M` по-прежнему корректны

## 4. Кэш → naive

- [x] 4.1 В `src/data/cache.py` оставить `_naive()`/`to_naive` как защиту на границах готовности свечи; убедиться, что `_observed`, `_last_loaded`, `boundary` — naive
- [x] 4.2 Обновить unit-тесты `tests/unit/data/test_cache.py`: границы naive, дозагрузка не падает при смешанных входных временах

## 5. Выбор инструментов и API → naive

- [x] 5.1 В `src/instruments/selector.py` — локальные `now`/`cutoff` naive-UTC (`datetime.now(timezone.utc).replace(tzinfo=None)`); внешний `expiration_date` сводится к naive через `to_naive` для сравнения
- [x] 5.2 В `src/api/instruments.py` тестовый запрос свечей идёт с aware `from_`/`to` (как принимает клиент; исходный `now()` уже aware)

## 6. Проверка внешнего API (R1)

- [x] 6.1 Проверка реального запроса к Tinkoff API: клиент НЕ принимает naive — причина регрессии «инструмент не найден». Исправлено добавлением `to_aware_utc` на исходящую границу API (loader `from_`/`to`, api `from_`)
- [x] 6.2 Прогнать `pytest` (unit + snapshot): снапшот-стратегии не изменились, все тесты зелёные
- [x] 6.3 Прогнать `ruff check` по изменённым файлам
