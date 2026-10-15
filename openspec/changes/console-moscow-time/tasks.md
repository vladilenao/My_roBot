## 1. Воспроизведение

- [ ] 1.1 В `tests/unit/notifier/test_templates.py` добавить сценарий из требования «Московское время внутри консольных строк»: группа `market_data_status` с `observed_at` 08:00 UTC и удержанием `execution_barrier` с `through` 08:00 UTC. Ожидание — `SBER · 15м · Удержано: базовая 1m не подтверждена до 11:00`, без `UTC` и без даты. Тест падает на текущем коде.
- [ ] 1.2 Найти все тесты, которые передают в консоль смещение, отличное от `3` (`test_templates.py`, `test_channels.py`, `test_console_event_golden.py`, `test_live_analysis.py`, `test_live_causal_processing.py`), и отметить, какие проверяют именно смещение в консоли — они переписываются на правило D1.

## 2. Поле удержания в событии

- [ ] 2.1 `LivePairState.hold` и его выдача в `payload`; `Event.market_data_status(..., hold=None)`; `hold` в `_OPTIONAL` схемы `market_data_status`. Проверить, что `Event.to_dict()` сериализует моменты `hold` (ключ события Telegram считается от `json.dumps(event.to_dict())`).
- [ ] 2.2 `LiveAnalysis`: `_execution_barrier` и `_hold_reason` возвращают `hold` по таблице D3 (моменты tz-aware UTC, `through` = закрытие бара); `reason` больше не содержит фразы удержания; пометка неизвестного расписания остаётся в `reason`. Барьер из цикла причинной обработки записывает `hold`, а не `reason`.
- [ ] 2.3 `StatusCadence.due` включает `hold` в подпись состояния.
- [ ] 2.4 Unit-тесты: каждый вид удержания даёт ожидаемый `hold` и пустой `reason`; удержание при неизвестном расписании; смена `through` вызывает новое сообщение, неизменное удержание — нет. `test_live_causal_processing.py` проверяет `hold.kind == "gap"` вместо текста «не восстановлен» в `reason`.

## 3. Консоль по Москве

- [ ] 3.1 В `console_view.py` — `clock(moment, anchor)` по D2; `when` переводит любой момент заголовка в UTC+3 без смещения; `_bar_clock` заменён на `clock`.
- [ ] 3.2 Фразы удержания по D4 — общая функция в `src/notifier/templates/` с форматтером момента; `status_row` вызывает её с `clock` и якорем группы.
- [ ] 3.3 `status_label` и `ConsoleChannel._handle_status` считают `hold` видимой причиной; ключ 15-минутного повтора включает `hold`.
- [ ] 3.4 Убрать `tz_offset_hours` из `ConsoleChannel`, `render`, `render_status_group`, `status_row`; фабрика не передаёт смещение в консоль.
- [ ] 3.5 Unit-тесты по сценариям дельты: удержание 1m, другой день (`14.10 23:45`), другой год, пропуск `parked` с `next_retry_at`, изменённая исполненная минута, задержка свечи, решение с `bar_time` при смещении `0` → консоль `09:00`, компьютер в другом поясе. Ни одна строка консоли в тестах не содержит `UTC` и ISO-меток.

## 4. Telegram

- [ ] 4.1 `telegram_templates.render` для `market_data_status` строит фразу удержания из `hold` той же общей функцией с форматтером `dated`; правило пояса Telegram не меняется.
- [ ] 4.2 Unit-тест: удержание `execution_barrier` в Telegram показывает полную дату и подпись пояса по смещению.

## 5. Документация и завершение

- [ ] 5.1 Обновить описание консольной ленты в `docs/` (если там есть примеры с `UTC` или со смещением в консоли) и поле `hold` в описании событий.
- [ ] 5.2 Запись в `CHANGELOG.md`.
- [ ] 5.3 Полный набор `tests/`, Ruff рабочего кода и тестов, `openspec validate console-moscow-time --strict`.
- [ ] 5.4 Визуальная приёмка: отрендерить `ConsoleChannel` на фикстурных событиях (удержание 1m на 31 пару, пропуск `parked`, сигнал с `bar_time`) в scratchpad и приложить вывод к отчёту для владельца. Полный робот на копии рабочих баз не запускается.
