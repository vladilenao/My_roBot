## 1. База ядра `core.sqlite3`

- [x] 1.1 `src/assembly/core_state.py`: схема (`metadata`, `contours`, `contour_commands`), `schema_version`, запись запуска и остановки с поколениями, повтор без нового поколения, журнал команд, чтение только на чтение для контура
- [x] 1.2 Тесты `tests/unit/assembly/`: поколения 1-2-3, повтор остановки `ignored`, `close_all` поверх `no_entries`, обратный переход отклонён, отклонённый запуск в журнале, переживает переоткрытие

## 2. Менеджер сделок

- [x] 2.1 `TradeManager.block_entries(mode)`: отказ новым входам и доборам с причиной `contour-stopping`, трасса решения
- [x] 2.2 `TradeManager.stop_actions(mode)`: `CancelEntry` неисполненных входов, для `close_all` — `CloseTrade` открытых позиций, без повторов при заявке в пути; счётчики открытых позиций и неисполненных входов
- [x] 2.3 Тесты `tests/unit/`: сигнал во время остановки, закрыть всё при уже идущем закрытии, добор запрещён, повторное применение без повторных действий

## 3. Сторона контура

- [x] 3.1 `src/assembly/contour_control.py`: чтение желаемого из `core.sqlite3` (`mode=ro`), применение при старте и смене поколения, `contour_state` в `trades.sqlite3`, завершение при пустом остатке, предупреждение при нечитаемой базе ядра
- [x] 3.2 Хук в `_run_bot`: применение до `runtime.post_tick`, итог после; флаг остановки `StoppableRunSession` общий со `StdinStop`
- [x] 3.3 `status.json`: атомарная запись после тика и при выходе
- [x] 3.4 Тесты: остановка без входов при открытой позиции, последняя сделка закрылась → выход 0, остановка без сделок, сбой посреди применения без повторного закрытия, битая база ядра

## 4. Сторона ядра

- [x] 4.1 `LiveCore.run_contour` → `prepare` + `launch`; `PatternTrigger.set_pairs` и инструменты опроса обязательств при повторном запуске
- [x] 4.2 Главный поток — цикл команд: запуск, надзор, после выхода снова принимает `POST`; Ctrl+C — `stop` в stdin без смены желаемого
- [x] 4.3 `LaunchControl` на `core.sqlite3`: желаемое, общее состояние по правилу design §3, `actual` из `status.json`, `waiting_command` после рестарта с прежним желаемым
- [x] 4.4 Тесты: запуск после остановки другим набором без второго `serve`, `409 STOPPING` во время дожития, рестарт ядра с желаемым «работает»

## 5. API и Swagger

- [x] 5.1 `export_api.py`: `GET /api/v1/contours/{id}`, `POST /api/v1/contours/{id}/stop`, `GET /api/v1/contours/{id}/log?tail=N`; ответы 200/202/400/404/409; ключ управления, `Host`, без CORS
- [x] 5.2 Swagger `docs/openapi.yaml` `1.5.0-proposal`: маршруты, примеры ответов, схемы `ContourStopRequest`, `ContourDesired`, `ContourActual`, `ContourStopAccepted`, новые состояния и коды `STOPPING`; проверка валидатором
- [x] 5.3 Тесты API: 202/200/409/404/400 остановки, лог с хвостом и границами, `401` ключом экспорта, `403` чужого `Host`, секрет не попадает в ответ лога

## 6. Документы и проверка

- [x] 6.1 `docs/architecture/core-contours.md` и артефакт документа 1: П4 ✓, что вынесено в П5/П6
- [x] 6.2 `README.txt` (остановка и повторный запуск командами), `CHANGELOG.md`
- [x] 6.3 Полный `pytest`, `ruff` (F821/F401), `openspec validate add-control-api --strict`
- [x] 6.4 Живая проверка на 8102: запуск → `stop no_entries` → дожитие → `stopped` → запуск другим набором → `stop close_all` → `stopped`; лог через API
