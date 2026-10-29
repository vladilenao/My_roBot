## 1. Настройки и события

- [x] 1.1 `[contours.sandbox]`: `telegram_bot_env`, `telegram_channel_env`, `reconcile_every_min` в `config_loader.py`, `config.py`, `default.toml`; `bindings` и незнакомые ключи — `ConfigError` с подсказкой.
- [x] 1.2 Токен бота из `telegram_bot_env` — в `_SECRETS` робота и контура.
- [x] 1.3 Тип события `broker_account` в `src/events/types.py` и схемах событий, шаблон Telegram для `started`, `reconcile_alarm`, `reconcile_cleared`, `stopped`; `docs/notification/event-schemas.md`.

## 2. Пакет `src/broker_contour/`

- [x] 2.1 `store.py`: `broker.sqlite3` — отпечаток счёта, отчёты сверки (последние 100).
- [x] 2.2 `fingerprint.py`: sha256 номера, последние четыре символа, сравнение и отказ «Журнал контура принадлежит другому счёту …XXXX».
- [x] 2.3 `admission.py`: допуск по порядку требования; отказ — код 2, `status.json` `failed` с `reason`, тревога в канал для пунктов 3–5.
- [x] 2.4 `reconcile.py`: сравнение счёта с журналом (в С2 журнал пуст), заморозки, переходы «совпадает ↔ расхождение», пропуск при недоступном счёте и тревога на третий подряд.
- [x] 2.5 `notify.py`: события `broker_account` в шину, Telegram-канал контура с ботом и каналом из настроек и своим `telegram_delivery.sqlite3`.
- [x] 2.6 `process.py`: шаг 10 с — пульс, целевое состояние, сверка по расписанию, `status.json`; остановка завершает процесс на шаге применения; `close_all` счёт не трогает.

## 3. Ядро

- [x] 3.1 `run.py --contour sandbox`: папка, блокировка, лог, `launch.json`, сборка процесса со шлюзом и правилом счёта из `src/sandbox`.
- [x] 3.2 `src/assembly/broker_supervisor.py`: поток надзора — запуск по команде, подхват и автоподъём по `plan_recovery`, отметка выхода, `stop` на Ctrl+C.
- [x] 3.3 `run.py`: надзиратель решает судьбу песочницы после готовности ядра и до `_serve_emulation`; неподтверждённый pid песочницы не мешает эмуляции.
- [x] 3.4 Состав из привязок в `contour_members`: карточка привязки, ключ — `id`.

## 4. API

- [x] 4.1 `src/bot/sandbox_control.py`: состояние, целевое, состав, отчёт сверки, лог; `ETag`/`If-Match`, коды ошибок по требованию.
- [x] 4.2 Маршруты `/api/v1/sandbox*` в `src/data/export_api.py`, `_links`/`_actions` в `control_views.py`, сводка `sandbox` в `GET /api/v1`.
- [x] 4.3 `GET /api/v1/sandbox/bindings`: `members` и `available` с `trades_in`.
- [x] 4.4 Черновик `openspec/changes/add-sandbox-contour/openapi.yaml` (2.1.0) переносится в `docs/api/openapi.yaml` вместе с обработчиками; контрактные тесты `test_launch_control.py` — на 2.1.0, `sandbox` в сводке `GET /api/v1`; `docs/api/README.md`.

## 5. Тесты

- [x] 5.1 `tests/unit/broker_contour/`: допуск (нет токена, бота, канала; два счёта; чужой отпечаток; первый старт), сверщик (ручная позиция, заявка, стоп-заявка, устранено, без повтора тревоги, недоступный счёт три раза), шаг (новое поколение, остановка, `close_all` не трогает счёт), отсутствие торговых вызовов шлюза.
- [x] 5.2 Надзиратель: запуск по команде, подхват, автоподъём раньше эмуляции, падение песочницы не трогает эмуляцию.
- [x] 5.3 API: запуск, `422 BINDINGS_REJECTED`, `409 CORE_NOT_READY`, `412`, привязки с `trades_in`, отчёт сверки, сводка в `GET /api/v1`; секреты и полный номер счёта не встречаются в ответах и файлах папки.
- [x] 5.4 Настройки: значения по умолчанию, `bindings` в секции, частота вне диапазона.
- [x] 5.5 Полный `pytest` и `ruff check`.

## 6. Ручная проверка владельцем

- [ ] 6.1 Робот запущен, `PUT /api/v1/sandbox/target` с `{"mode": "on", "bindings": ["<id привязки NG>"]}` → в канале «Песочница» сообщение о старте со счётом …09c3 и капиталом; `GET /api/v1/sandbox` показывает тот же капитал, что `python -m src.sandbox account`.
- [ ] 6.2 `python -m src.sandbox buy NG 1` → не позже чем через минуту в канале одна тревога «NG: на счёте 1, по журналу 0, заморожен»; `GET /api/v1/sandbox/reconcile` показывает то же.
- [ ] 6.3 Робот перезапущен (Ctrl+C и запуск) → в консоли песочница поднимается раньше эмуляции, в канале «старт» и снова тревога по NG.
- [ ] 6.4 `python -m src.sandbox sell NG 1` → в канале «расхождение устранено», заморозки нет.
- [ ] 6.5 `GET /api/v1/sandbox/bindings` показывает все привязки конфигурации, у привязки NG — `trades_in` с `sandbox` (и `emulation`, если NG в эмуляции).
- [ ] 6.6 `PUT /api/v1/sandbox/target` с `{"mode": "close_all"}` → контур остановлен, ручные позиции на счёте (если есть) не тронуты, эмуляция работает дальше.
