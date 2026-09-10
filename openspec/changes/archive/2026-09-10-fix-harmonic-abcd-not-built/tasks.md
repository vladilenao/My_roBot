## 1. Включение harmonic_abcd в штатную карту стратегий

- [x] 1.1 В `run.py` добавить импорт `DEFAULT_CONFIG` из `src.strategies.harmonic_abcd_strategy` и запись `"harmonic_abcd": HARMONIC` в `_strategy_map()` (паттерн как у существующих стратегий)

## 2. Fail-fast проверка согласованности привязок с картой стратегий

- [x] 2.1 В `src/bot/trading_bot.py` в `_validate()` (после `validate_assignments`) добавить проверку: каждое имя стратегии из значений `_share_strategies` и `_future_strategies` обязано быть ключом `_strategy_map`; при отсутствующих — `ValueError` с перечислением имён до входа в цикл

## 3. Тесты

- [x] 3.1 В `tests/unit/bot/test_trading_bot.py` добавить unit-тест: запуск с привязкой имени, отсутствующего в `strategy_map`, падает с `ValueError` до цикла, запросы данных не выполняются (по образцу `test_fail_fast_on_unknown_strategy`)
- [x] 3.2 Добавить unit-тест: согласованная привязка и `strategy_map` с `harmonic_abcd` — стратегия строится (кэш содержит экземпляр, factory вызывается для неё)
- [x] 3.3 Аудит существующих вызовов `TradingBot(...)` в тестах (`tests/unit/bot/test_trading_bot.py` и др.): фикстуры с рассинхронными `strategy_map`/привязками привести в согласованность, чтобы новая проверка их не ломала

## 4. Проверки

- [x] 4.1 Прогнать pytest (все unit-тесты) — зелёные
- [x] 4.2 Прогнать ruff по изменённым файлам — без замечаний