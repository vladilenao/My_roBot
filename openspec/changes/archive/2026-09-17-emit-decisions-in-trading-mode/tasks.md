## 1. Code Fix

- [x] 1.1 Добавить вызов `_emit(...)` в ветке `trade_manager` в `src/bot/trading_bot.py:269-280` перед `continue`
- [x] 1.2 Добавить тест-регрессия `test_decisions_notified_even_in_trading_mode` в `tests/unit/bot/test_trading_bot.py`

## 2. Spec & Documentation

- [x] 2.1 Создать OpenSpec-чейндж `emit-decisions-in-trading-mode` (proposal, design, delta spec)
- [x] 2.2 Записать MODIFIED-требование «Формат уведомления» в `orchestration`-спеке: доставка каждого решения на каждом тике в торговом режиме
- [x] 2.3 Синхронизировать спеку в основной `openspec/specs/orchestration/spec.md` (openspec-sync-specs)

## 3. Verification

- [x] 3.1 Запустить `pytest tests/unit/bot/test_trading_bot.py -q` — все тесты, включая новый, пройдены
- [x] 3.2 Запустить `pytest tests/ -q` — общая проверка
- [x] 3.3 Запустить линтер, если настроен, и убедиться в отсутствии проблем