## 1. Форматтер (src/notifier/base.py)

- [x] 1.1 Добавить параметр `tz_offset_hours: float = 0.0` в конструктор `DecisionFormatter` и сохранить его.
- [x] 1.2 В `format()` формировать блок времени как `(bar_time + timedelta(hours=offset)).strftime("%H:%M")`, не меняя внутреннего `decision.bar_time`.
- [x] 1.3 Добавить unit-тесты в `tests/unit/notifier/test_formatter.py`: нулевое смещение выводит UTC как раньше; `+3` сдвигает `06:15` → `09:15`; отрицательное смещение работает; отсутствие `bar_time` по-прежнему опускает блок времени.

## 2. Конфигурация и проводка (src/config.py, src/notifier/__init__.py)

- [x] 2.1 Добавить `BAR_TIME_TZ_OFFSET_HOURS = 0` в `src/config.py`.
- [x] 2.2 В `get_notifier()` собирать `DecisionFormatter(tz_offset_hours=BAR_TIME_TZ_OFFSET_HOURS)` и передавать его в конструктор выбранного нотификатора (покрывает telegram и console).

## 3. Проверка

- [x] 3.1 Прогнать `pytest tests/unit` — все модульные тесты проходят.
- [x] 3.2 Прогнать `ruff check` по изменённым файлам.
- [x] 3.3 Прогнать `openspec validate --specs`.
