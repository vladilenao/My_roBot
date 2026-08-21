## 1. Окружение и структура

- [x] 1.1 Создать `requirements-dev.txt`: запинить фактически установленные версии pandas, pandas-ta-classic, numba, pytest (`pip freeze` выборочно); убедиться, что основной `requirements.txt` не изменён
- [x] 1.2 Создать дерево `tests/data/macd_rsi_stoch/` и заглушку `tests/strategies/__init__.py` (при необходимости для пакета)
- [x] 1.3 Зарегистрировать кастомную опцию pytest `--update-snapshots` в `tests/conftest.py` (`pytest_addoption`) — только объявление флага, без логики

## 2. Скрипт скачивания фикстур (контур A)

- [x] 2.1 Написать `tools/download_snapshot_data.py`: параметры CLI — тикер, тип инструмента, таймфрейм, стратегия, имя кейса
- [x] 2.2 Реализовать расчёт глубины: импорт `SIGNAL_WINDOW` из `src.config`, `depth = min(max(SIGNAL_WINDOW × 4, WARMUP_RESERVE), 300)`; константа `WARMUP_RESERVE = 50`
- [x] 2.3 Сформировать явные даты запроса: `end = now()`, `start = end − depth × длительность таймфрейма`; передать обе даты в `load_candles` (не полагаться на дефолт 30 дней)
- [x] 2.4 Добавить пост-обрезку `tail(depth)` и проверку `len(df) <= depth`
- [x] 2.5 Записать `tests/data/<strategy>/<case>/candles.csv`: столбцы `datetime,open,high,low,close,volume`, datetime ISO без tz, полная float-точность
- [x] 2.6 В том же скрипте рассчитать эталон: `tech_analyze(df)` → `rolling(SIGNAL_WINDOW).sum()` по сигналам → консенсус BUY/SELL → записать `expected_signals.csv` (datetime, signal, price=close, macd_sum, rsi_sum, stoch_sum)
- [x] 2.7 Скачать первый кейс: NG (future), 1h → `tests/data/macd_rsi_stoch/NG_1h/`; проверить глазами оба CSV и число строк ≤ 300

## 3. Проверочный тест (контур B)

- [x] 3.1 Создать `tests/strategies/test_macd_rsi_stoch.py`: сканирование `tests/data/macd_rsi_stoch/*/` и динамическая параметризация по папкам кейсов (id = имя подпапки); пустая папка → ноль кейсов без ошибок
- [x] 3.2 Фикстуры чтения данных: `candles.csv` → DataFrame с приведением типов; `expected_signals.csv` → эталонный DataFrame
- [x] 3.3 Реализовать replay: `tech_analyze(df)` → `rolling(SIGNAL_WINDOW).sum()` по `macd_signal/rsi_signal/stoch_signal` с окном из `src.config` (импорт, не хардкод)
- [x] 3.4 Выделить события консенсуса: все три суммы строго одного знака → BUY/SELL, price = close; отсечь NaN-регион прогрева индикаторов (~35 бар)
- [x] 3.5 Реализовать сверку: `pandas.testing.assert_frame_equal(actual, expected, check_exact=False, rtol≈1e-9)` по столбцам datetime/signal/price(+суммы); сообщение об ошибке указывает первую расходящуюся строку
- [x] 3.6 Реализовать ветку `--update-snapshots`: вместо сверки перезаписать `expected_signals.csv` актуальными событиями; без флага файлы не изменяются

## 4. Проверка и стабильность

- [x] 4.1 Прогнать `pytest tests/strategies/ -v` offline: кейс NG_1h проходит; убедиться, что сетевых вызовов нет (тест работает без `TINKOFF_TOKEN`)
- [x] 4.2 Негативная проверка детерминизма: временно изменить вход (например подменить одну цену в копии candles.csv) → тест падает с внятным diff; вернуть обратно
- [x] 4.3 Проверить регенерацию: `pytest --update-snapshots` на неизменном коде даёт пустой diff эталона
- [x] 4.4 Прогнать весь набор `pytest` (старые юнит-тесты + новые snapshot) — ничего не сломано
