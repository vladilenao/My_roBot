## 1. Пульс ядра

- [x] 1.1 `heartbeat` в `core_state.py`: `beat(pid, version, started_at)`, `mark_stopped()`, `read_heartbeat(path)` только на чтение (`mode=ro`)
- [x] 1.2 Поток пульса ядра: первый удар до подхвата и запуска контура, удар раз в `heartbeat_secs`, `stopped` при остановке ядра до ожидания контура
- [x] 1.3 `[contours] heartbeat_secs`, `heartbeat_timeout_secs`, `autostart` в `config.py`, `config_loader.py`, `default.toml`; проверка `timeout > secs`
- [x] 1.4 Тесты: удары, `stopped`, чтение старой базы без таблицы, отказ конфига

## 2. Менеджер сделок

- [x] 2.1 `TradeManager.hold_entries(bool)`: отказ входам и доборам `core-lost`, неисполненные входы и закрытия не трогаются, приоритет остановки контура и снятия инструмента; подписи в `models.py` и `trade_journal/reasons.py`
- [x] 2.2 Тесты: сигнал при запрете, вход сохраняется, добор запрещён, снятие запрета при `no_entries`, повтор без записей

## 3. Контур

- [x] 3.1 `CorePulse` (поток контура): `ok` / `lost` / `stopped` / чужая версия, журнал переходов; `alive_at` в `status.json` под замком `ContourControl`
- [x] 3.2 `StdinStop`: конец потока только в журнал, `stop` и Ctrl+C как прежде
- [x] 3.3 `ContourControl.before_tick`: запрет `core-lost` по состоянию связи; `core_link` в `status.json`; `exited_at` и `exit_reason` при выходе
- [x] 3.4 Тесты: потеря и возврат связи, `stopped` и чужая версия останавливают после тика, конец stdin не останавливает, `alive_at` без тиков, `exited_at` при штатном выходе

## 4. Ядро: старт и подхват

- [x] 4.1 `recovery_plan(...)`: живой контур (pid + `status.json` + `alive_at`) той же или другой версии, неподтверждённый процесс, автоподъём, ожидание команды, явный запуск
- [x] 4.2 `AdoptedProcess(pid)`: `running`, `pid`, `exit_code = None`, `stop()` по пульсу с принудительным завершением через 30 с (POSIX `SIGKILL`, Windows `TerminateProcess`); итог по `exited_at`
- [x] 4.3 Ожидание выхода контура другой версии, затем запуск своей
- [x] 4.4 Автоподъём в `run.py` и `LiveCore.run_contour`: состав из `contour_members` и последнего `status.json`, карточки заново с запасным `contract_json`, `launch.json` с текущим поколением без `record_launch`
- [x] 4.5 Явный запуск при подхваченном контуре: сообщение в консоли, желаемое не меняется
- [x] 4.6 `LaunchControl`: `recovery`, источник и команда из желаемого, `failed` с `exit_code = null`, `actual.core_link`
- [x] 4.7 Тесты: все ветви `recovery_plan`, подхват и команды стопа и состава через базу, остановка подхваченного, другая версия, неподтверждённый pid не завершается, автоподъём с `removed`-участником, `autostart = false`

## 5. API

- [x] 5.1 `GET /api/v1/core`: версия, pid, старт, пульс, сводка `pair_readiness_all` и ленты, `RequestBudget.snapshot()`; доступ по ключу управления
- [x] 5.2 Swagger `docs/openapi.yaml` `1.7.0-proposal`: маршрут и схема `CoreState`, `Contour.recovery`, `ContourActual.core_link`, примеры; проверка валидатором
- [x] 5.3 Тесты API: ответ без секретов, `401` с ключом экспорта, сверка схемы

## 6. Документы и проверка

- [x] 6.1 `docs/architecture/core-contours.md` и артефакт документа 1: П6 ✓, нюансы «Ядро упало, контур жив», «После рестарта ядра», «Обновление версии»
- [x] 6.2 `README.txt`, `CHANGELOG.md`: автоподъём после обновления и `[contours] autostart`
- [x] 6.3 Полный `pytest`, `ruff` (F821/F401), `openspec validate add-contour-recovery --strict`
- [x] 6.4 Живая проверка на 8102, три рестарта:
  - Ctrl+C при открытой сделке → старт с `--wait-command` → автоподъём, сделка сопровождается;
  - `kill -9` ядра → `core_link: lost`, входов нет → новое ядро подхватывает, второго процесса нет;
  - смена версии → старый контур выходит, новый поднимается с прежним составом
