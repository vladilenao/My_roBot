# Tasks — fix-candle-polling-storm

## 1. Троттлинг форс-дозагрузки и ограниченное окно

- [x] 1.1 В `src/data/cache.py` добавить глобальный страж последнего обращения к API
  (`_last_api_attempt`) и параметр `data_refresh_min_interval`; в
  `refresh_if_new_candle` (включая `force=True`) пропускать вызов `_load`, если
  интервал с последнего обращения к API меньше минимума.
- [x] 1.2 В `refresh_if_new_candle`/`ensure_loaded` вычислять `start_date =
  max(last_dt, now - data_backfill_window)` для инкрементальной/принудительной
  дозагрузки (bounded backfill); первичная загрузка пары остаётся сессионной.
- [x] 1.3 Unit-тесты кэша: форс-дозагрузка не чаще интервала (API не вызывается до
  истечения `data_refresh_min_interval`); окно дозагрузки ограничено; после истечения
  интервала дозагрузка выполняется и свежий бар фиксируется.

## 2. Конфигурация новых параметров

- [x] 2.1 В `src/config.py` (`_DEFAULTS`) добавить `data_refresh_min_interval`
  (default 5) и `data_backfill_window_seconds` (default 3600), пробросить в модульные
  константы с доступом из `run.py`.
- [x] 2.2 В `run.py` передать значения в `MarketDataCache(...)` (параметры с
  дефолтами, инжектируемые в тесты).

## 3. Ретрай потока свечей

- [x] 3.1 В `src/api/retry.py` вынести парсинг `ratelimit_reset` в публичный хелпер
  `rate_limit_reset_secs(exc) -> float | None`; `_parse_reset_delay` переиспользовать
  поверх него.
- [x] 3.2 В `src/data/loader.py` обернуть итерацию `client.get_all_candles(...)`:
  при `RESOURCE_EXHAUSTED` во время итерации повторно открывать поток с экспоненциальной
  паузой, ограниченной `ratelimit_reset`, сохраняя уже собранные свечи; по исчерпании
  повторов — пробрасывать последнюю ошибку.
- [x] 3.3 Unit-тесты: `rate_limit_reset_secs` читает `ratelimit_reset=N` и возвращает
  `None` при его отсутствии; итерация потока при rate-limit повторно открывает поток
  и доводит загрузку без потери строк; после `max_retries` ошибка пробрасывается.

## 4. Учёт лимита API

- [x] 4.1 В `src/data/cache.py` при `RESOURCE_EXHAUSTED` фиксировать
  `_retry_after = now + max(reset, DEFAULT_BASE_DELAY)` и пропускать дозагрузки до
  этого момента; по истечении возобновлять (использовать `rate_limit_reset_secs`;
  без подсказки сброса — фиксированная пауза).
- [x] 4.2 В `src/bot/trading_bot.py` при ошибке тика с `RESOURCE_EXHAUSTED` выбирать
  паузу `min(fallback_secs(), reset)`, логируя ограничение; в остальных случаях —
  как раньше.
- [x] 4.3 Unit-тесты: кэш не дозагружает до `_retry_after` и возобновляет после;
  пауза после ошибки тика учитывает `ratelimit_reset` и не превышает fallback.

## 5. Проверка

- [x] 5.1 Обновить затронутые unit-тесты кэша/планировщика под новую семантику
  (`force` не снимает интервал; готовность проверяется по кэшу).
- [x] 5.2 `.venv/bin/python -m pytest -q` — зелёный.
- [x] 5.3 `.venv/bin/python -m ruff check .` — без новых ошибок (пре-существующие в
  `tools/` и `openspec/_check_sync.py` допустимы).