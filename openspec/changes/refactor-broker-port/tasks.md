Каждый шаг — отдельный коммит с зелёным полным `pytest`. Существующие тесты меняют импорты и сборку объектов (фейки, фикстуры), проверки не меняются.

## 1. Шаг 1 — переносы без логики

- [x] 1.1 `src/broker_contour/port.py`: общие типы — отчёты, `OrderFailure`, `BrokerCallError`, `AccountRuleError`, `RobotAccount`, `AccountSnapshot`, `mask_account`.
- [x] 1.2 Пакет `src/broker_contour/tinvest/`: `api.py`, `gateway.py`, `account.py`, `admission.py` — тела функций без изменений. Реэкспорты в `src/sandbox/gateway.py` и `src/sandbox/account.py`. Удалить `broker_contour/api.py`, `broker_contour/account.py`, `account/account.py`, `account/admission.py`; импорты кода и тестов — на новые места.
- [x] 1.3 Полный `pytest` и `ruff check src tests run.py`, коммит.
  Сделано 24.11.2026 (251edec): 4109 passed, как до переноса; `ruff` чистый.

## 2. Шаг 2 — порты

- [x] 2.1 `port.py`:
  - протоколы `OrderPort`, `StopPort`, `MarketPort`, `OrderFeed`, `MarginLimits`, `FeeSource`, `BrokerSession`;
  - `StopQuery`, `StopMatch`, `PriceBook`;
  - отчёты в пунктах: `BrokerTrade` без `in_rub`, `OrderReport` с `average` вместо рублёвых сумм.
- [x] 2.2 `tinvest/port.py` — `TInvestPort` поверх `TInvestApi`:
  - перевод рублей в пункты по `PriceBook`;
  - `order_state` — выбор поиска по ключу или по номеру, как в `_refresh`;
  - `poll_stops` — поиск по номеру и атрибутам из `stops.py`, дословно;
  - `MarginLimits` для «Счёта».

  Сюда же `tinvest/prices.py` (`points_from_rub`, `fill_average`, `trade_price`) и `tinvest/fees.py` (`FeeSync`).
- [x] 2.3 `tinvest/session.py` — `TInvestSession`: правило счёта, факты, снимок, капитал, имена, `trading()`, `fee_source()`, `fee_probes()`; `tinvest.adapter("sandbox" | "account", …)`.
- [x] 2.4 `TInvestBroker` → `LiveBroker(port, ledger, *, book, margin_limits=None, fills_prefix, margin_every, …)`. `PriceBook` выделен из `LiveBroker`; `StopKeeping` и `OrderStream` — на узких портах; `BrokerContour(settings, session_factory, …)` и `admit_account(session, …)` — на `BrokerSession`.
- [x] 2.5 Граница: `tests/unit/broker_contour/test_broker_port.py` — общий код не импортирует `t_tech`, `grpc`, `src.sandbox`, `src.broker_contour.tinvest`, `src.broker_contour.finam` (AST, и внутри функций).
- [x] 2.6 Контрактный набор `tests/unit/broker_contour/contract/`, параметризованный адаптером, на `TInvestPort(FakeApi)`:
  - вход и исполнение;
  - цель;
  - стоп у брокера и срабатывание;
  - потерянные ответы на заявку и стоп;
  - рестарт с досверкой;
  - снятие.
- [x] 2.7 Старая база: `broker.sqlite3` и `trades.sqlite3` с исполнениями `tinvest:` и `tinvest-order:` → рестарт, брокер повторяет те же сделки → новых исполнений нет; `FeeSync` находит ждущие комиссии.
- [x] 2.8 Средняя заглушки в адаптере: GAZP (лот 10, `lots_executed` в штуках) — та же цена, что прежде.
- [x] 2.9 Полный `pytest` и `ruff`, коммит.
  Сделано 24.11.2026: 4131 passed и 1 failed — `test_glitched_portfolio_skips_reconcile_and_keeps_stop_guard_snapshot`; он нестабилен и на `release-12` без этого change (3 падения из 12 прогонов, лишняя тревога `reconcile_alarm` по времени), отдельно, вне change. Проверки существующих тестов не менялись: в диффе тестов нет строк с `assert`.

## 3. Шаг 3 — описание контура

- [x] 3.1 `src/broker_contour/contours.py`: `LiveContour`, `LIVE_CONTOURS` («Счёт», песочница), чтение `[contours.<номер>]` по номеру.
- [x] 3.2 `run.py`:
  - одна функция запуска контура на брокере, сборка адаптера по описанию;
  - надзиратели циклом по описаниям, `BrokerContourSupervisor(contour_id, kind)`;
  - `kind` в `status.json` из описания;
  - `CONTOUR_TITLES` из описаний.
- [x] 3.3 Тесты:
  - `status.json` и `launch.json` у `sandbox` и `account` прежние;
  - заголовки «ПЕСОЧНИЦА» и «СЧЁТ»;
  - «Счёт» поднимается первым;
  - у каждого контура свой каталог, база, отпечаток и канал.
- [x] 3.4 Полный `pytest` и `ruff`, коммит.
  Сделано 24.11.2026: `tests/unit/broker_contour/test_live_contours.py`; `_run_broker_contour(номер)` — одна функция по описанию, адаптер выбирает `_broker_sessions`, надзиратели — `_broker_supervisors`. Слой API (`SandboxControl`, `AccountControl`) получает номер контура от надзирателя, остальное в нём — Ф2. `_contour_portfolio_pct` и `_contour_commission_pct` оставлены прежними: на их константы опирается `test_contour_config`.

## 4. Сборка

- [ ] 4.1 PyInstaller `run.spec` собирает с пакетом `tinvest/`; собранный `--contour sandbox` на тестовых данных доходит до допуска без `ImportError`.

## 5. Ручной регресс владельца в песочнице Т-Инвест (до ПРОМ)

Тестовый робот из PyCharm на 8102. ПРОМ (8101, `~/Desktop/my_roBot_app`) не трогать.

- [ ] 5.1 Рестарт с открытой позицией: стоп у брокера не задваивается, исполнения за простой проводятся.
- [ ] 5.2 Сверка и уточнение комиссий.
- [ ] 5.3 Предохранители: срабатывание и сброс.
- [ ] 5.4 `close_all`.
- [ ] 5.5 Сравнение песочницы с эмуляцией.
- [ ] 5.6 «Счёт» на чтение (`orders = false`) стартует и сверяется.
