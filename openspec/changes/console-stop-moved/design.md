## Context

- `ConsoleChannel.handle` (`src/notifier/console.py`) получает `stop_moved` и передаёт его в `render` из `src/notifier/templates/console_view.py`. Ветка торговых событий `_execution` разбирает `TARGET_HIT`, `TRADE_CLOSED`, `ORDER_REJECTED`, `ORDER_ACCEPTED`, `TRADE_OPENED`/`POSITION_ADDED`, `STOP_HIT`, `TRADE_CANCELLED`, `PROTECTION_ARMED`, `RISK_LIMIT_HIT`, `CLEARING_DONE`. Остальное получает `return None`, поэтому `stop_moved` молча не печатается.
- `Block.render` переносит значение через `textwrap.TextWrapper`, разбивая только по ASCII-пробелам. Неразрывный пробел U+00A0 не считается точкой переноса: проверено на ширине 60 — пара `1.1183 → 1.1199` уходит на следующую строку целиком.
- `stop_moved` публикует `run.py:publish_execution`: тип `STOP_MOVED` по действию `MOVESTOP`, поля `quantity` (0 у ACK), `price` (нет у ACK), `reason`, `status`, `execution_id`, `bar_time` = момент вступления, `visual`. Финансовые поля (`gross_pnl`, `quantity_remaining` и др.) передаются только для `fill`/`partial`.
- `lifecycle_snapshot` (`src/trade_management/notification.py`) уже содержит всё нужное: `state.average_entry`, `state.quantity`, `state.stop`, `stops[...]` с `old`/`new`/`reason` по `event_key`, `price_step`, `side`. Стоимость шага есть в строке `trades.step_cost`, но в снимок не выводится.
- `REASON_LABELS` (`src/trade_journal/export.py`) знает `atr-trailing-stop` и `cost-aware-break-even`, но не `ma-cloud-protective-stop`. `reason_label` возвращает неизвестный код как есть.

## Goals / Non-Goals

**Goals:**
- Перенос стопа виден в консоли блоком, согласованным с ВХОД/СДЕЛКА/ЦЕЛЬ, по утверждённому эталону.
- Уровни, остаток и денежная оценка передаются в событии явными полями, шаблон консоли не разбирает `visual`.
- Узкий терминал не разрывает пару цен.

**Non-Goals:**
- Изменение Telegram.
- Общая защита денежных чисел от переноса.
- Группировка переносов.

## Decisions

### D1. Поля события в `publish_execution`

Для `MOVESTOP` с подтверждённым вступлением (`status=ack`, событие публикуется после effective time — правило не меняется) `publish_execution` добавляет поля из `details["visual"]`:

- `stop` = `stops[event_key].new`, `previous_stop` = `stops[event_key].old` (если не `None`);
- `quantity_remaining` = `state.quantity`;
- `pnl_at_stop` = `d × (stop − average_entry) × quantity × step_cost / price_step`, `pnl_units` = `RUB`.

`step_cost` выводится в снимок `lifecycle_snapshot` рядом с `price_step` (`trades.step_cost`). Без `average_entry`, `price_step` или `step_cost` поле `pnl_at_stop` не передаётся, а `pnl_units` = `RAW`. Расчёт — отдельная чистая функция рядом с `lifecycle_snapshot`, чтобы её проверял unit-тест без шины.

*Альтернатива:* читать `visual` в шаблоне консоли. Отклонена: шаблон консоли не зависит от формата снимка графика, а спека требует явных уровней в событии.

### D2. Схема события

В `OPTIONAL_PAYLOAD_FIELDS[STOP_MOVED]` добавляются `previous_stop`, `quantity_remaining`, `pnl_at_stop`, `pnl_units` (`stop` и `reason` уже есть в `_EXECUTION_OPTIONAL`). Типы в `_JSON_TYPES`: цены и деньги — как у `stop`/`gross_pnl`. Документы `docs/notification/event-schemas.md` и `docs/json-schemas.md` обновляются тем же способом, что и при прошлых изменениях схемы.

### D3. Блок консоли

В `_execution` новая ветка `STOP_MOVED`:

- `label = "ЗАЩИТА"`, `role = ""`, время — общий `stamp` (`bar_time` → Москва);
- значение — `f"{name} · стоп перенесён {old} → {new}"` либо `f"{name} · стоп перенесён на {new}"`; цены — `price()` без округления;
- подробность — части через ` · `: причина (D4), `остаток N`, денежная часть по знаку `pnl_at_stop` (`money(abs(x), estimate=True)`, ноль — `0 ₽` без `~`); первая буква строки — заглавная;
- `_financial(event)` к блоку не добавляется: у переноса нет исполнения и комиссий.

### D4. Причины

`REASON_LABELS["ma-cloud-protective-stop"] = "подтяжка за облаком MA"`. Для консоли — функция подписи переноса: `cost-aware-break-even` → развёрнутая фраза требования; иначе `reason_label(code)`, если результат отличается от кода; иначе часть опускается. Подпись журнала не меняется для `cost-aware-break-even` («безубыток всей сделки»).

### D5. Неразрывная пара в `Block.render`

Шаблон склеивает пару U+00A0. `Block.render` после переноса заменяет U+00A0 на обычный пробел во всех строках, поэтому в выводе служебных символов нет. Цветовая подсветка (`highlight`) работает по уже заменённой строке; для `ЗАЩИТА` роли нет.

### D6. Эталон

Буквальные блоки сценариев требования «Перенос стопа в консоли» проверяются в `tests/unit/notifier/test_console_event_golden.py` через `_golden(scenario)`, как эталон торговых событий. До архивации тест читает дельту изменения, после — главную спеку; путь выбирается так же, как в прошлых изменениях, либо тест переключается на главную спеку при архивации.

## Risks / Trade-offs

- **Поля дублируют `visual`.** Небольшая избыточность ради независимости шаблонов; источник один — снимок после подтверждения.
- **Длинная строка безубытка переносится.** Она шире 96 колонок; эталон фиксирует перенос с отступом.
- **Оценка без проскальзывания.** `~` и «до комиссий» говорят, что это оценка по цене стопа, а не гарантированный результат.
