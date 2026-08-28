## 1. Модель инструмента и селектор

- [x] 1.1 В `src/instruments/model.py` добавить полю `short_name` в dataclass `Instrument` (по умолчанию `ticker` при отсутствии).
- [x] 1.2 Расширить `normalize_instrument`: принимать 2-, 3- и 4-кортежи; для 4-кортежа `short_name` — четвёртый элемент, иначе — тикер.
- [x] 1.3 В `src/instruments/selector.py` `fetch_active_futures` возвращает 4-кортежи `(display, ticker, "future", contract_name)` — краткое имя контракта как `short_name`.
- [x] 1.4 Вспомогательные функции отбора/валидации/дедупликации в `selector.py` обрабатывают 3- и 4-кортежи без учёта количества элементов; интерактивное меню (`_format_futures_display`) не меняется.

## 2. Форматтер, фабрика и порт

- [x] 2.1 `DecisionFormatter` (`src/notifier/base.py`): добавить параметр `timeframe=""`; при непустом значении после `<инструмент>` через один пробел выводится блок `(<timeframe>)`.
- [x] 2.2 `get_notifier()` (`src/notifier/__init__.py`): подставлять `TIMEFRAME` из конфигурации в `DecisionFormatter`.
- [x] 2.3 `NotifyOnlyExecutionPort` (`src/execution/port.py`): брать `instrument.short_name` (при отсутствии — `instrument.label`).

## 3. Тесты и проверка

- [x] 3.1 Обновить `tests/unit/notifier/test_formatter.py`: форматтер с `timeframe="1h"` даёт `● NG-10.26 (1h) 22:00 | ...`; добавить сценарий «без таймфрейма — блок (tf) опускается».
- [x] 3.2 Обновить `tests/unit/notifier/test_notifiers.py`: ожидаемые сообщения с учётом таймфрейма форматтера.
- [x] 3.3 Обновить `tests/unit/instruments/test_model.py`: `short_name` для 3-кортежа = тикер, для 4-кортежа = contract_name.
- [x] 3.4 Обновить `tests/unit/instruments/test_selector.py`: `fetch_active_futures` возвращает 4-кортежи с `short_name`; выбор по номерам/тикерам не ломается.
- [x] 3.5 Обновить `tests/unit/execution/test_port.py`: порт передаёт в `notify_decision` `short_name`.
- [x] 3.6 Полный прогон в venv (`python -m pytest`) и линт (`ruff check src tests`) — зелёный.