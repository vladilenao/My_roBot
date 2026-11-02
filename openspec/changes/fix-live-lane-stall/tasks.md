## 1. Сторож полос

- [x] 1.1 `src/data/lane_watchdog.py`: `LaneWatchdog` — `begin(name)` / `end(name)` из потока полосы, `check(now)` находит шаги дольше порога, `snapshot()` для API; стек потока через `sys._current_frames` и `traceback.format_stack`.
- [x] 1.2 `src/data/poller.py`: `_run_lane` и `_run_live_worker` обрамляют `step()` вызовами `begin` / `end` в `try/finally`; поток `market-data-watchdog` запускается и останавливается вместе с полосами.
- [x] 1.3 Тревога: один WARNING со стеком на зависший шаг и строка в консоль ядра; по завершении шага — INFO с длительностью.
- [x] 1.4 Сводка «Полосы данных: запросы за минуту…» пишется из потока сторожа; при зависших полосах в конце строки — «зависли: <полоса> с <время>».

## 2. Конфигурация и API

- [x] 2.1 `src/data/ingestion_settings.py`: ключ `lane_stall_timeout_secs` (по умолчанию 300, конечное положительное число), ошибка с именем ключа.
- [x] 2.2 `src/assembly/core.py`: `core_snapshot` отдаёт `lanes` из `watchdog.snapshot()`; до `ready` — пустой объект.
- [x] 2.3 `docs/market-data/live-ingestion.md`: параметр, текст тревоги, поле `lanes` в `GET /api/v1/core`; `docs/api/openapi.yaml` (схема `Core.lanes`) и `CHANGELOG.md`.

## 3. Тесты

- [x] 3.1 `tests/unit/data/lanes/`: шаг полосы блокируется на событии дольше порога (фиктивные часы) — WARNING один раз, со стеком функции шага; после освобождения — INFO; `snapshot()` показывает `stalled`, затем `ok`.
- [x] 3.2 Пауза между шагами (простой полосы полноты) дольше порога тревогу не вызывает.
- [x] 3.3 Сводка полос выходит, пока шаг живой полосы заблокирован, и называет зависшую полосу.
- [x] 3.4 Конфиг: неположительный и нечисловой `lane_stall_timeout_secs` отклоняется с именем ключа; без ключа — 300.
- [x] 3.5 `GET /api/v1/core`: поле `lanes` со `state`, `step_started_at`, `last_step_at`; в ответе нет секретов.
- [x] 3.6 Полный `pytest` и `ruff check`.

## 4. Живая проверка

- [ ] 4.1 На тестовом роботе (8102) с временным `lane_stall_timeout_secs = 5` и искусственной задержкой шага живой полосы: в журнале есть WARNING со стеком, в консоли — строка тревоги, в `GET /api/v1/core` — `lanes.live.state = stalled`; после снятия задержки — INFO и `ok`. Сводка полос выходит всё время.
