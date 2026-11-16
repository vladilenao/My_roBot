## 1. Шлюз и правило счёта

- [x] 1.1 `src/sandbox/gateway.py`: общий `BrokerGateway` (дедлайн, повторы `RESOURCE_EXHAUSTED`, записи вызовов, лог с видом контура); `SandboxGateway` — как сейчас; `AccountGateway` — `Client` на адресе брокера, `sandbox.*` → `BrokerCallError(SANDBOX_ONLY)` до сети (design.md решение 2).
- [x] 1.2 `src/account/account.py`: правило счёта вида `account` — `GetAccounts`, один открытый счёт, номер равен `TINKOFF_ACCOUNT_ID`, `access_level` (решение 3); номер регистрируется секретом.
- [x] 1.3 Фейковый брокер для счёта в `tests/unit/sandbox/fakes.py` или рядом: счета с `access_level`, `GetInfo`, `GetUserTariff`, без сервиса песочницы; юнит-тесты 1.1–1.2.

  Сделано 11.11: `BrokerGateway` / `SandboxGateway` / `AccountGateway` в `src/sandbox/gateway.py`, правило счёта `src/account/account.py` (`RobotAccount.access_level`), фейк — `users.get_info`, `tariff_fails`, `qualified` в `tests/unit/sandbox/fakes.py`.

## 2. Допуск и первый старт

- [x] 2.1 `src/broker_contour/admission.py`: допуск вида `account` по требованию «Допуск контура «Счёт» к старту» — права токена по режиму, `GetInfo` (тариф, `qualified_for_work_with`, допуск к срочным контрактам), `GetUserTariff`, предупреждения при `orders = false`; факты допуска в `status.json` (`admission`).
- [x] 2.2 Отпечаток вида `account`; непустой счёт при первой записи отпечатка — тревога «Счёт не пуст на первом старте» и заморозка инструментов (решение 5).
- [x] 2.3 Карточка `started` «СЧЁТ» с капиталом, правами токена и предупреждениями.
- [x] 2.4 Юнит-тесты допуска: два счёта, чужой номер, только чтение, нет допуска к срочным контрактам при `orders = false` и `true`, подмена счёта по отпечатку, непустой счёт на первом старте.

  Сделано 11.11: `src/account/admission.py` (`read_facts`), `admit_account(find_account=…, read_facts=…)`, `Admitted.facts` → `status.json.admission`; первый старт на непустом счёте — причина первой тревоги сверщика (`Reconciler.note`), заморозка — общим путём сверщика; карточка `started` — права токена и предупреждения.

## 3. Процесс и ядро

- [x] 3.1 `run.py --contour account`: сборка `BrokerContour` с `AccountGateway`, правилом счёта и допуском, без торговой части; `TINKOFF_TRADE_TOKEN` и `TINKOFF_ACCOUNT_ID` в `secrets=` ядра и контура.
- [x] 3.2 Настройки `[contours.account]` в `config_loader.py`, `config.py`, `default.toml`: `telegram_bot_env`, `telegram_channel_env`, `reconcile_every_min`, `orders` (только `false`, иначе `ConfigError` с подсказкой про Р2).
- [x] 3.3 Ядро: второй `BrokerContourSupervisor` вида `account`; порядок подъёма «Счёт» → «Песочница» → эмуляция; отказ одного не задерживает следующие (решение 6).
- [x] 3.4 Юнит-тесты: только чтение (нет `PostOrder`, `PostStopOrder`, `Cancel*` в записях вызовов), порядок подъёма, настройки.

  Сделано 11.11: `run.py --contour account` (`_run_account_contour`), секреты `TINKOFF_TRADE_TOKEN`, `TINKOFF_ACCOUNT_ID`, бот «Счёта» в `_SECRETS`; `[contours.account]`; второй надзиратель и `_recover_broker_contours(account, sandbox)` перед эмуляцией.

## 4. Разведка комиссий на чужой сделке

- [x] 4.1 `fee_probes` в `broker.sqlite3`: через 15 минут после изменения чужой позиции — `GetOperationsByCursor` по инструменту, ответы П25–П26 (решение 8); сбой — тип ошибки, не более трёх повторов.
- [x] 4.2 Юнит-тесты: связанная «комиссия брокера», без связи, без комиссии, сбой чтения.

  Сделано 11.11: `src/account/fee_probes.py` (`FeeProbes`, таблица `fee_probes`, `read_fee_probes`), вызов из сверки «Счёта» только на чтение.

## 5. API

- [x] 5.1 Ресурс `/api/v1/account` в `src/bot/` и `src/data/export_api.py` по требованию «Ресурс контура «Счёт»»: `admission` в `current`, `PUT /target` без `bindings`, `close_all` с `confirm`, `409 ACCOUNT_READ_ONLY` для состава, `fee_probes` в `reconcile`, ресурсы торговли; `account` в `GET /api/v1` и в отметках привязок.
- [x] 5.2 `docs/api/openapi.yaml` 2.11.0 по проекту `openapi.yaml` этого change, тег «Счёт».
- [x] 5.3 Юнит-тесты API: запуск, `bindings` в теле — `400`, `close_all` без `confirm` — `409`, состав — `409`, факты допуска, ответ по схеме OpenAPI.

  Сделано 11.11: `src/bot/account_control.py` (`AccountControl` поверх `SandboxControl`: ссылки, действия, `admission`, `fee_probes`, `CONFIRM_REQUIRED`, `ACCOUNT_READ_ONLY`), маршруты в `export_api.py`, `account` в `GET /api/v1`; `docs/api/openapi.yaml` 2.11.0 = проект change; `ReconcileReport` получил недостающее поле `in_flight`. Тесты — `tests/unit/bot/test_account_api.py`.

## 6. Секреты и прогон

- [x] 6.1 Тест: после старта и сверки значения `TINKOFF_ACCOUNT_ID` и токена нет в логе, `status.json`, `broker.sqlite3`, записях вызовов.
- [x] 6.2 Полный `pytest` и `ruff check src tests run.py`.

  Сделано 11.11: тесты — `tests/unit/broker_contour/test_account_contour.py` (20), `tests/unit/assembly/test_contour_config.py` (+7), `tests/unit/bot/test_account_api.py` (8); полный `pytest` 4126 passed, `ruff check` чистый.

## 7. Ручная проверка владельцем

Перед проверкой: отдельный брокерский счёт робота, токен только на чтение и только на него, деньги на счёте; в `.env` — `TINKOFF_TRADE_TOKEN`, `TINKOFF_ACCOUNT_ID`.

  Правки по первому запуску 16.11 (счёт …7029): в карточке старта «Счёта» строка «Режим: только чтение счёта»; лимиты в `admission.limits` — по методам контура (`PostOrder`, `PostStopOrder`, `GetOperationsByCursor`), а не минимум по сервису (у операций он 5 — отчёт брокера).

- [ ] 7.1 Первый старт на пустом счёте: в канале «Счёт» карточка `started` с капиталом, как в приложении Т-Инвест; `GET /api/v1/account` — права `read_only`, тариф, список допусков.
- [ ] 7.2 Сделка руками в приложении: за минуту тревога сверки «на счёте +1, по журналу 0», инструмент заморожен; через 15 минут в `GET /api/v1/account/reconcile` запись `fee_probes` с ответами П25–П26.
- [ ] 7.3 Чужой номер счёта в `TINKOFF_ACCOUNT_ID`: контур не стартует, причина в канале и API.
- [ ] 7.4 Рестарт робота с автоподъёмом: «Счёт» поднимается раньше песочницы и эмуляции.
- [ ] 7.5 Неделя чтения: ни одной ложной тревоги сверки, капитал в API совпадает с приложением.
