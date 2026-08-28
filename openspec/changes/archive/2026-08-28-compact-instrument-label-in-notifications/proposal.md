## Why

Уведомления о решениях содержат громоздкую подпись инструмента вида `BR (Нефть Brent) — BR-10.26`, что перегружает строку. Пользователь хочет компактный префикс `BR-10.26 (1h)` — краткое читаемое имя контракта и таймфрейм, одинаково в консоли и Telegram.

## What Changes

- `Instrument` получает поле `short_name` (краткое имя для уведомлений): для фьючерсов — `contract_name` (первое слово имени контракта из API, напр. `BR-10.26`), для остальных — тикер (`SBER`). Полный `label` сохраняется для интерактивного меню и логов, меню НЕ меняется.
- `fetch_active_futures()` возвращает кортежи, несущие краткое имя; функции отбора/валидации в `src/instruments/selector.py` пробрасывают его. Итоговый список инструментов — кортежи из 4 элементов `(display_name, ticker, instrument_type, short_name)`.
- `normalize_instrument()` и конструктор `Instrument` принимают `short_name` (по умолчанию = тикер).
- `DecisionFormatter` получает параметр `timeframe`; префикс сообщения меняется на `● <short_name> (<timeframe>) HH:MM | <стратегия> ➜ <сигнал>`.
- `get_notifier()` (фабрика `src/notifier/__init__.py`) подставляет таймфрейм из конфигурации `TIMEFRAME` в форматтер; формат применяется и в консоли, и в Telegram (форматтер общий).
- `NotifyOnlyExecutionPort` берёт `instrument.short_name` вместо `instrument.label`.

## Capabilities

### New Capabilities
- нет

### Modified Capabilities
- `instruments`: формат возвращаемого списка инструментов дополняется кратким именем (`short_name`/`contract_name`) для уведомлений; меню и полный display сохраняются.
- `notification`: префикс строки форматтера становится `● <инструмент> (<таймфрейм>) HH:MM | ...`, где `<инструмент>` — краткое имя (`short_name`), а таймфрейм добавляет сам форматтер.

## Impact

- Код: `src/instruments/selector.py`, `src/instruments/model.py`, `src/notifier/base.py`, `src/notifier/__init__.py`, `src/execution/port.py`.
- Затрагиваются тесты: `tests/unit/notifier/test_formatter.py`, `tests/unit/notifier/test_notifiers.py`, `tests/unit/instruments/test_model.py`, `tests/unit/instruments/test_selector.py`, `tests/unit/execution/test_port.py`.
- API/инструменты: формат кортежа инструментов меняется с 3 на 4 элемента (внутренний код проекта; внешних контрактов нет).
- Спеки: `instruments`, `notification`.