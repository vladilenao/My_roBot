## 1. Воспроизведение

- [x] 1.1 Unit-тест `tests/unit/trade_management/test_entry_filter_admission.py`: `TradeManager` с `SignalFilter()`, привязка `basic_levels`, тренд `down`, BUY → сейчас `OpenTrade` (ПЛАН). Тест падает на текущем коде.

## 2. Вердикт фильтра

- [x] 2.1 `SignalFilter.judge` и `FilterVerdict` (D2).
- [x] 2.2 `BasicLevelsFilter.rejection_text` (D4).
- [x] 2.3 `TripleScreenFilter.apply_with_trace` и `rejection_text` (D3).
- [x] 2.4 Unit-тесты причин в `tests/unit/decision/`.

## 3. Допуск

- [x] 3.1 `TradeManager._plan_entry`: фильтр первым шагом, трасса со ссылкой на привязку, отказ `filter-rejected` (D1, D5); текст кода по умолчанию в `_REJECTION_MESSAGES`.
- [x] 3.2 `LiveAnalysis`: сырой сигнал в допуск при наличии `actions_for_signal` (D6).
- [x] 3.3 Unit-тесты: basic_levels против тренда/по тренду, triple_screen с непройденным экраном, противоположный сигнал по открытой сделке не фильтруется, live публикует СИГНАЛ и передаёт сырое решение.

## 4. Завершение

- [x] 4.1 Запись в `CHANGELOG.md`.
- [x] 4.2 Полный `pytest`, Ruff, `openspec validate entry-filter-admission --strict`.
