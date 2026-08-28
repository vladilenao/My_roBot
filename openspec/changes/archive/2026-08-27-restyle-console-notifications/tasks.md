## 1. Поле времени в решении

- [x] 1.1 В `src/strategies/contracts.py` добавить поле `bar_time: pd.Timestamp | None = None` в dataclass `Decision` (tz-naive, время последнего бара)

## 2. Компактный формат уведомления

- [x] 2.1 В `src/notifier/base.py` переработать `DecisionFormatter.format`: однострочная строка вида `● <label> HH:MM | <стратегия> ➜ <сигнал>`, маркер `● ` всегда
- [x] 2.2 BUY → `🟢 ПОКУПКА (BUY) — Цена: X`; SELL → `🔴 ПРОДАЖА (SELL) — Цена: X`; HOLD → `⏳ Нет сигнала.` без цены; цена с точностью 3 знака
- [x] 2.3 Опускать блок `HH:MM` при отсутствии `bar_time` и блок `| <имя>` при отсутствии `strategy_name`; сегменты разделять пробелами
- [x] 2.4 Убрать отдельные строки `Таймфрейм:` и `Индикаторы:` из вывода

## 3. Заполнение времени бара в оркестраторе

- [x] 3.1 В `src/bot/trading_bot.py::_analyze` после `strategy.decide(...)` подставить `bar_time` из `frame["datetime"].iloc[-1]` через `dataclasses.replace(decision, bar_time=...)` (протокол стратегий не меняется)

## 4. Тесты

- [x] 4.1 Обновить `tests/unit/notifier/test_formatter.py` на новый формат: покупка/продажа с `bar_time` и стратегией, HOLD без цены, отсутствие `bar_time`, отсутствие `strategy_name`
- [x] 4.2 Обновить `tests/unit/notifier/test_notifiers.py` (текст доставки) и проверить unit-тесты, конструирующие `Decision` (`test_signals.py`, `test_port.py`, `test_trading_bot.py`) — дефолт `bar_time=None` не ломает их; обновить ожидания при необходимости

## 5. Проверка

- [x] 5.1 Прогнать `pytest` (unit + snapshot) — все тесты зелёные, снапшот-стратегии не изменились
- [x] 5.2 Прогнать `ruff check` по изменённым файлам