## Context

См. proposal.md — «Why». Текущая цепочка формирования строки уведомления:

```
fetch_active_futures (instruments/selector.py:30-66)
  contract_name = f.name.split()[0]   ← краткое имя контракта ("BR-10.26")
  display = f"{base_name} ({label}) — {contract_name}"  ← сшито в label
  3-кортеж (display, ticker, "future")

→ Instrument(label, ticker, instrument_type)  ← краткое имя теряется

→ NotifyOnlyExecutionPort.execute → label = instrument.label (весь display)

→ DecisionFormatter.format(decision, instrument_label)
     "● {instrument_label} HH:MM | {strategy} ➜ {signal}"  ← форматтер не знает таймфрейм
```

Краткое имя живёт только внутри строки label, а таймфрейм (`TIMEFRAME`) до уведомлений не доходит. Это и есть два источника правки.

## Goals / Non-Goals

**Goals:**
- Краткое имя инструмента как самостоятельные данные (`short_name`), доступные от селектора до форматтера.
- Таймфрейм попадает в строку через конструктор форматтера (как уже сделано с часовым поясом).
- Формат `● <short_name> (<timeframe>) HH:MM | ...` одинаков для консоли и Telegram (форматтер общий) и для акций, и для фьючерсов.
- Интерактивное меню, полный `label` и логи не меняются.

**Non-Goals:**
- Не меняем `bar_time`, часовой пояс и логику сигналов.
- Не вводим отдельный формат для Telegram (общий форматтер — осознанно).
- Не трогаем отображение в меню выбора (`_format_futures_display` остаётся).

## Decisions

1. **`short_name` — данные, а не парсинг (решение A2)**
   `Instrument` получает поле `short_name`. Для фьючерсов при выборе это `contract_name` из API; для акций и прочего — тикер. `normalize_instrument` принимает кортежи 2/3/4 элементов: для 3-кортежа `short_name = ticker`, для 4-кортежа — четвёртый элемент.
   *Альтернатива A1:* извлекать краткое имя из label (`label.rsplit(" — ", 1)[-1]`). Отклонено — парсинг привязан к формату `_format_futures_display` и молча сломается при его изменении; данные теряться не должны.

2. **Таймфрейм в конструкторе `DecisionFormatter` (решение Б1)**
   `DecisionFormatter(timeframe="", tz_offset_hours=0.0)`; блок `(<timeframe>)` выводится только при заданном значении. Фабрика `get_notifier()` подставляет `TIMEFRAME`. Форматтер остаётся единственным местом сборки строки.
   *Альтернатива Б2:* собирать `"BR-10.26 (1h)"` в порту/оркестраторе и передавать уже готовым label. Отклонено — размазывает форматирование по слоям и заставляет порт знать про таймфрейм.

3. **Порт берёт `short_name`**
   `NotifyOnlyExecutionPort.execute` использует `getattr(instrument, "short_name", None) or getattr(instrument, "label", "")` — если объект без `short_name`, остаётся прежнее поведение (обратная совместимость для тестов).

4. **Селектор пробрасывает краткое имя**
   `fetch_active_futures` собирает записи `(display, ticker, "future", contract_name)`; вспомогательные функции (`_select_from_list`, `_validate_instruments`, `_deduplicate`) обрабатывают 3- и 4-кортежи без знания количества элементов. Меню (`_format_futures_display`) не меняется.

## Risks / Trade-offs

- **Ручной ввод фьючерсов по тикеру** (в меню пишут "BRU6") → `short_name = ticker` = "BRU6", без читаемого имени контракта. Mitigation: приемлемо (это ручной случай), стандартные фьючерсы выбираются из списка с полным именем.
- **Смена паттерна label в меню** → на уведомления не влияет (краткое имя теперь самостоятельное поле, не парсится).
- **4-кортеж как формат списка** → внутренний формат проекта; кортежи создаются/потребляются только в `selector.py` и `normalize_instrument`, внешних потребителей нет (проверено: `run.py`, `tests`).
- **Пропущенный таймфрейм у форматтера** (оставлено по умолчанию "") → блок `(tf)` не выводится, строки для тестов без таймфрейма остаются валидными.

## Migration Plan

Внутрипроектное изменение без внешнего контракта: 4-кортежи создаются и потребляются только в `src/` и `tests/`. Откат — revert изменений `model.py`, `selector.py`, `notifier/base.py`, `notifier/__init__.py`, `execution/port.py`.

## Open Questions

Нет.