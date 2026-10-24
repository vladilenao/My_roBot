## 1. Контракты в `docs/api/`

- [ ] 1.1 `docs/api/openapi-draft.yaml` → `docs/api/openapi.yaml` версии `2.0.0`: убрать `x-replaces`, `/api/v1/live`, `LiveContour`, справочник `/api/v1/instruments` и его схемы; `docs/openapi.yaml` удалить
- [ ] 1.2 `docs/market-events.asyncapi.yaml` → `docs/api/market-events.asyncapi.yaml`
- [ ] 1.3 `docs/api/README.md` — обзор контрактов, ключей и Swagger; нынешний README проекта → `docs/api/v2-decisions.md` (план «Счёта» и справочника остаётся там)
- [ ] 1.4 Пути: `src/data/export_api.py` (`_OPENAPI`), `run.spec`, `tests/unit/clusters/test_report.py`, `tests/unit/market_structure/test_asyncapi_contract.py`, ссылки в `docs/market-data/README.md`, `docs/market-data/emulator-integration.md`, `docs/trade-management/signal-pipeline.md`, `docs/market-events.md`, `docs/database.md`, комментарий `default.toml`
- [ ] 1.5 Тесты: `/openapi.yaml` отдаёт `docs/api/openapi.yaml` версии `2.0.0`, в контракте нет маршрутов без обработчика и обработчиков без маршрута

## 2. Маршруты и точка входа

- [ ] 2.1 Маршрутизация `export_api.py`: `/api/v1`, `/api/v1/emulation…`, `/api/v1/history…`; `/api/v1/contours…` — `404 NOT_FOUND`
- [ ] 2.2 `GET /api/v1`: оба ключа, сводка ядра, эмуляции и истории по правам ключа, `_links` по правам, `live = null`
- [ ] 2.3 `GET /api/v1/core`: `_links`
- [ ] 2.4 Тесты: прежний маршрут — `404`, точка входа с каждым ключом и без ключа, при неготовом ядре

## 3. Ошибки `problem+json`

- [ ] 3.1 Ответ ошибки управления: `type` `urn:my-robot:problem:<code>`, `title`, `status`, `code`, `detail`; `rejections[].instrument`, `core`, `pid`
- [ ] 3.2 Граница по маршруту: экспорт и кластеры — прежний `{code, message}`, включая `401`
- [ ] 3.3 Тесты: каждый код управления — `problem+json`; ошибки экспорта не изменились

## 4. Эмуляция

- [ ] 4.1 Представление эмуляции: `state`, `target` (`mode` из `running` + `stop_mode`, `revision`), `current`, `launch` (`request` объектом), `process`, `instruments` (`target`/`current` участников); в ответах нет `desired`, `actual`, `generation`
- [ ] 4.2 `PUT /api/v1/emulation/target`: разбор `mode` и `instruments` (только с `on`), запуск → `202`, остановки → `202`/`200`, `409`/`422` как прежде; `GET …/target` — `404`, если эмуляцию не запускали
- [ ] 4.3 `POST /api/v1/emulation/instruments` `{instruments}` и `POST …/instruments/remove` `{instruments, mode}` поверх `change_membership`; ответ — состав с `revision`; `GET …/instruments`, `GET …/instruments/{name}`
- [ ] 4.4 `GET /api/v1/emulation/log`
- [ ] 4.5 Тесты: запуск, повтор, остановки и переходы режимов, состав пачкой и строгий отказ, удаление имени не из состава, `current` отстаёт от `target`

## 5. Имена инструментов эмуляции

- [ ] 5.1 `classify_tokens` с видом команды: в эмуляции код семейства и биржевой тикер — отказ с коротким именем ближайшего допустимого контракта или этого контракта; `all` — как прежде; история — без изменений
- [ ] 5.2 Удаление сопоставляет имя только с `name` участника
- [ ] 5.3 Тесты: `NG` и `NGZ6` в запуске и добавлении — `422` с подсказкой `NG-12.26`, `all` выбирает ближайшие, история с `NG` — цепочка

## 6. История

- [ ] 6.1 `id` прогона в API — имя папки; поиск по имени папки, строки прежней версии читаются
- [ ] 6.2 `GET /api/v1/history` (`?state=active|finished`, `?limit=`, `limits` с `max`), `POST`, `GET /{id}` с `progress` и `process`, `POST /{id}/stop` (`202` / повтор `200`), `GET /{id}/report` (`404 REPORT_NOT_READY`), `GET /{id}/log`
- [ ] 6.3 Тесты: очередь и список, итог только у завершённого, стоп из очереди и идущего, строка прежней версии с префиксом

## 7. Ссылки, действия, ревизия

- [ ] 7.1 Одна функция допустимых действий для `_actions` и проверки команды: эмуляция, участник, прогон
- [ ] 7.2 `_links` в точке входа, ядре, эмуляции, участнике, прогоне, списках
- [ ] 7.3 `ETag` в ответах эмуляции; `If-Match` в командах эмуляции под той же блокировкой, что запись; `412 REVISION_CHANGED`, журнал — `conflict`
- [ ] 7.4 Тесты: для каждого состояния действие из `_actions` принимается, а действие не из списка отклоняется; две команды с одним `If-Match`

## 8. Консоль и документы

- [ ] 8.1 Консоль: «Робот ждёт команду запуска: PUT …/api/v1/emulation/target», «Повторить запуск: PUT /api/v1/emulation/target {…}», подсказка при прежних параметрах называет оба маршрута
- [ ] 8.2 `README.txt` (запуск и остановка, примеры тел), `CHANGELOG.md` (таблица «было → станет»), `docs/architecture/core-contours.md` (таблица API)
- [ ] 8.3 `openspec validate rework-contour-api --strict`, полный прогон тестов

## 9. Живая проверка владельцем

- [ ] 9.1 Запуск эмуляции `{"mode": "on", "instruments": ["NG-12.26", "SBER"]}`, отказ `["NG"]` с подсказкой, добавление и удаление пачкой, `no_entries` → `close_all`
- [ ] 9.2 `If-Match` со старой ревизией — `412`; `_actions` совпадают с тем, что принимает сервер
- [ ] 9.3 Прогон истории: очередь, ход, стоп, итог `/report`, `id` по имени папки
- [ ] 9.4 Swagger `/docs` 2.0.0; экспорт и эмулятор данных работают как прежде
