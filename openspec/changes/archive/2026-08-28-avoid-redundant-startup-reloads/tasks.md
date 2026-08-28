## 1. Оркестратор: проверяем бар по кэшу, а не безусловной перекачкой

- [x] 1.1 Изменить `TradingBot._bar_is_ready()` (`src/bot/trading_bot.py:112`): сначала возвращать `True`, если `data_cache.has_fresh_closed_bar()` уже даёт свежий бар; `refresh_if_new_candle(force=True)` вызывать только при отсутствии бара, после чего повторно проверять готовность.

## 2. Кэш UID инструмента

- [x] 2.1 В `MarketDataCache` (`src/data/cache.py`) добавить кэш UID: при первой успешной загрузке инструмента сохранять `instrument_id` из результата `load_candles` по ключу `(ticker, instrument_type)`.
- [x] 2.2 При повторных загрузках того же инструмента передавать закэшированный `instrument_id` в загрузчик и пропускать разрешение UID.
- [x] 2.3 В `load_candles` (`src/data/loader.py`) добавить опциональный параметр `instrument_id=None`: при переданном значении `find_working_instrument` не вызывается, UID используется для запроса свечей; возвращаемый кортеж сохраняет `(DataFrame, instrument_id)`.

## 3. Тесты и проверка

- [x] 3.1 Адаптировать тестовые дубли кэша в `tests/unit/bot/test_trading_bot.py` (дубли с `has_fresh_closed_bar`/`refresh_if_new_candle`) под early-exit: refresh теперь вызывается только при отсутствии бара; выровнять счётчики и ожидания `wait_boundaries`.
- [x] 3.2 Новый тест бота: запуск в середине периода — `refresh_if_new_candle(force=True)` НЕ вызывается (бар уже в кэше), решение доставлено.
- [x] 3.3 Новые тесты загрузчика/кэша: при переданном `instrument_id` `find_working_instrument` не вызывается; повторная загрузка инструмента использует закэшированный UID.
- [x] 3.4 Прогнать полный набор тестов в venv (`python -m pytest`) и линт (`ruff check src tests`), добиться зелёного прогона.