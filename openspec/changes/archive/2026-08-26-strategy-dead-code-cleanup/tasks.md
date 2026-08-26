## 1. Мёртвый код

- [x] 1.1 Удалить `EVENT_COLUMNS_TEMPLATE` из `src/strategies/macd_rsi_stoch.py`
- [x] 1.2 Удалить `STRATEGY_CONFIGS` из `src/scheduler/runner.py`
- [x] 1.3 Импортировать `DEFAULT_CONFIG` из `src.strategies.macd_rsi_stoch` в `runner.py`

## 2. Redundant class attributes

- [ ] 2.1 Удалить `NAME = "macd_rsi_stoch"` из `MacdRsiStochStrategy` — **ОТМЕНЕНО**: `@register` требует `NAME` на классе
- [ ] 2.2 Удалить `STRATEGY_WINDOW = 5` из `MacdRsiStochStrategy` — **ОТМЕНЕНО**: используется в методах класса

## 3. Верификация

- [x] 3.1 Запустить `pytest tests/unit/ -v` — все тесты пройдены
- [x] 3.2 Запустить `pytest tests/snapshot/ -v` — все тесты пройдены
