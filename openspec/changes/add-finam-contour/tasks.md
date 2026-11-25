Ветка `feature/finam-contour` от `origin/release/release-12`, без upstream. Push и PR делает владелец. Каждый шаг — коммит с зелёным полным `pytest` и чистым `ruff check src tests run.py`. Существующие тесты проверок не меняют. Баги проверки чинятся в этой ветке и записываются сюда.

## 0. Согласование

- [x] 0.1 Владелец согласовал proposal и design 24.11: свой бот `TELEGRAM_FINAM_BOT_TOKEN` и канал `TELEGRAM_FINAM_CHANNEL_ID`, каталог карточек на `TINKOFF_TOKEN`.

## 1. Заглушки Ф0

- [x] 1.1 `git merge --no-ff feature/finam-probe`. Полный `pytest` на слиянии.
  Сделано 24.11 (6cc43cd): 4392 passed.

## 2. Общий код без веток по брокеру — поведение «Счёта» и песочницы не меняется

- [x] 2.1 `port.py`:
  - `BrokerOutage(BrokerCallError)`;
  - `BrokerSession` (чтение) и `TradingSession` (капитал, имена, торговля);
  - `InstrumentCard`, `InstrumentCatalog`.
- [x] 2.2 `process.py`:
  - `_read(session, account)` с исходами «снимок / потерян / окно» вместо трёх перехватов в `_reconcile`, `_reconcile_trading`, `_final_account`;
  - допуск ждёт окна одним циклом.

  `reconcile.py`: пропуск в окне не считается. `admission.py`: `BrokerOutage` не превращается в отказ.
- [x] 2.3 `contours.py`: поля `provider`, `trading`, `defaults`; `broker` в `status.json`.
- [x] 2.4 Конфиг: ключи и проверки `[contours.<номер>]` контуров на брокере — по описаниям; `orders = true` при `trading = False` → `ConfigError`.
- [x] 2.5 `run.py`:
  - `_SECRETS` — из описаний;
  - сборка контура с `orders = true` только при `trading`.

  `console_view.py` — метка из `CONTOUR_TITLES`.
- [x] 2.6 Тесты 2.1–2.5:
  - `BrokerOutage` при допуске, сверке, остановке, остановка во время ожидания окна;
  - «Счёт» без `BrokerOutage` — прежние тревоги;
  - `ConfigError` для `orders` у описания без торговли;
  - значения `[contours.account]`/`[contours.sandbox]` прежние;
  - граница общего кода дополнена `src.finam`.

  Сделано 24.11 одним коммитом с разделами 3 и 4 (72902fc): описание `finam` связывает конфиг, тесты описаний и адаптер, по отдельности промежуточные коммиты были бы красными. Полный `pytest` на снимке коммита — 4470 passed, `ruff` чистый.
  Изменённые проверки существующих тестов — только из-за нового контура (новое требование «Описание контура на брокере»): порядок описаний, заголовки и надзиратели (`test_live_contours`), список контуров в сообщении `run.py` (`test_launch_entry`).

## 3. Каталог карточек ядра

- [x] 3.1 `tinvest/catalog.py` — `TInvestCatalog`: шлюз на `TINKOFF_TOKEN` только `instruments.*`, `by_uid`, `by_ticker`, короткое имя правилом `short_name_by_uid`, кэш на процесс.
- [x] 3.2 Тесты: карточки акции и фьючерса, отказ не-`instruments.*` до сети.

## 4. Адаптер `src/broker_contour/finam/`

- [x] 4.1 `transport.py`:
  - `FinamTransport`: канал, `call`, текст ошибки в одну строку;
  - повтор по `reset_time` (до 2, не дольше 65 с, без квоты — 60 с);
  - окно 05:00–06:15 МСК → `BrokerOutage`.
- [x] 4.2 `transport.py` — `JwtSource`: `Auth` при входе, стрим `SubscribeJwtRenewal`, секрет до использования, замок, переподписка 5/10/30/60 с, запасной `Auth` старше 10 мин; `AuthService` без заголовка.
- [x] 4.3 `symbols.py` — `FinamSymbols`: `symbol` по карточке (RTSX/MISX), карточка по `symbol`, `SymbolMismatch` по шагу и лоту, `margin(symbol)` без кэша.
- [x] 4.4 `account.py`:
  - правило счёта по `TokenDetails` (номера — секреты до проверки);
  - снимок: сумма RUB, нулевые позиции, лоты акций, активные заявки и стопы, `names` только по UID.
- [x] 4.5 `admission.py` — факты `access_level`, `limits`, `warnings`.
- [x] 4.6 `session.py` — `FinamSession` (`BrokerSession`), `adapter(secret, account_id, catalog_factory)`; `run.py` — `_broker_sessions` для `finam-account`.
- [x] 4.7 Описание `finam` в `contours.py`; `[contours.finam]` в `default.toml`; `run.spec` — `src.finam.proto.*` и `src.broker_contour.finam`.
- [x] 4.8 Тесты `tests/unit/broker_contour/finam/` на своём фейке Финама:
  - всё из 4.1–4.6;
  - граница адаптера: без разведки `src.finam`, `tinvest`, `src.sandbox`, `t_tech`.

## 5. API

- [x] 5.1 `AccountControl` — параметры экземпляра: контур, база, заголовок, провайдер, `orders_enabled`, `reconcile_extras`, сравнение; `routes()`; `broker` в сводке и `GET`.
- [x] 5.2 `comparison_view.py`: `AccountComparison` с именем своей стороны, `SIDES` от него.
- [x] 5.3 `export_api.py`:
  - таблица `LIVE` «базовый путь → ресурс» вместо `_account_get` / `_account_command` по `views.ACCOUNT`;
  - `GET /api/v1` по таблице;
  - ресурсы торговли `finam`.
- [x] 5.4 `run.py`: ресурсы `account` и `finam` с их частями; журнал торговли `finam`.
- [x] 5.5 `docs/api/openapi.yaml` 2.14.0:
  - тег «Счёт Финам», пути;
  - `Finam`, `FinamCurrent`, `FinamAdmission`;
  - `broker` у `Account`;
  - `account_link: maintenance`.

  Тест схемы.
- [x] 5.6 Тесты API: чтения `finam`, `409 ACCOUNT_READ_ONLY`, `404` сравнения и предохранителей, ключ `finam`, `broker`; ответы `account` и `sandbox` прежние.

  Сделано 24.11 (de3dd39): полный `pytest` на снимке коммита — 4479 passed, `ruff` чистый. Изменённые проверки: версия контракта 2.14.0 (`test_launch_control`); сборка ресурса «Счёта» в `test_account_api` передаёт разведку комиссий, как `run.py`.

## 6. Регресс — автотесты

- [x] 6.1 Маскирование: `FINAM_SECRET_TOKEN`, `FINAM_ACCOUNT_ID`, каждый JWT и номера счетов `TokenDetails` — нет в логе, `status.json`, событиях канала, ответах API.
- [x] 6.2 Два живых контура одновременно: `account` и `finam` — свои каталоги, базы, отпечатки; отпечаток `finam` не принимает чужой счёт.
- [x] 6.3 Конфиг `[contours.finam]`.
- [x] 6.4 Полный `pytest` и `ruff check src tests run.py`.
  6.1 — `test_finam_contour.py::test_secrets_nowhere`, `test_finam_transport.py`; 6.2 — `test_account_and_finam_side_by_side`, `test_foreign_fingerprint_refused_with_alarm`; 6.3 — `test_contour_config.py` (finam). Итог — 4479 passed.
- [x] 6.5 PyInstaller: `run.spec` собирает; собранный `--contour finam` без секрета отказывает «Не задан FINAM_SECRET_TOKEN».
  Сделано 24.11: сборка прошла, в PYZ есть `src.broker_contour.finam.*`, `src.broker_contour.tinvest.catalog` и заглушки `src.finam.proto`; собранный `run --contour finam` собрал адаптер без ошибок импорта и вышел с кодом 2 «Не задан FINAM_SECRET_TOKEN».

## 7. UI (репозиторий `UI`, ветка `feature/ui-finam-contour`)

- [x] 7.1 `TradingContour`, `BrokerContour` += `'finam'`; таблица `CONTOUR_BASE` в `queries.ts`.
- [x] 7.2 `AccountPage` с параметром `contour`, маршрут `/finam`, пункт меню «Счёт Финам» при ключе `finam` в `GET /api/v1`; блок допуска показывает общие поля и поля своего брокера, если они есть.
- [x] 7.3 Карточка в «Обзоре», строка в «Аварийной остановке», метка брокера в шапке.
- [x] 7.4 Типы из OpenAPI 2.14.0, `npm run build`; проверка на тестовом роботе 8102 — в 8.5.

  Сделано 24.11 в репозитории UI, ветка `feature/ui-finam-contour` (3bb6ecf): контракт 2.14.0, `npm run check` — 316 тестов, lint и формат чистые; `npm run build` собран. Маршрут — `/contours/finam`, как у остальных контуров (`/contours/account`). Сравнение контуров на странице Финама не показывается до Ф4. В браузере не проверено — проверка в 8.5.

## 8. Ручная проверка владельца — тестовый робот 8102, не ПРОМ

Пошаговая инструкция — `regress.md`.

**Багфиксы проверки:**
- **8.2, 25.11.** В фактах допуска `limits` пустые. Финам называет квоту `AccountsService.getAccount` (метод с маленькой буквы), а допуск сравнивал имена с `GetAccount` с учётом регистра. Фейк тестов отдавал имена не в формате Финама. Исправлено: квоты сравниваются без учёта регистра, фейк отдаёт имена как Финам.


- [x] 8.1 Контур `finam` стартует и проходит допуск на реальном счёте Финама. Карточка старта в канале Финама с меткой «СЧЁТ ФИНАМ» и последними 4 символами счёта.
  Пройдено 25.11 (тестовый робот 8102, f2895cd):
  - `running`, `broker = finam`, счёт …3922, 29 983,30 ₽, позиций и заявок нет, `account_link = ok`;
  - допуск `full_access`, квоты 200 (после багфикса квот);
  - карточка «СЧЁТ ФИНАМ · Контур запущен» доставлена;
  - JWT обновлён стримом;
  - в файлах `data/contours/finam/` нет секрета, номера счёта, токенов, id канала и JWT; ошибок в логе нет.
- [x] 8.2 Сверка «ок». Расхождение видно: ручная заявка владельца в терминале Финама → тревога сверки; заявка снята → сверка снова «ок».
  Пройдено 25.11:
  - сверка `ok`;
  - лимитная заявка Si владельца в терминале → на ближайшей сверке (22:49:48 МСК) `mismatch`: Si-12.26 «заявки — на счёте 1, по журналу 0», Si-12.26 заморожен, тревога «СЧЁТ ФИНАМ · ТРЕВОГА · Сверка счёта» в канале;
  - заявка снята → в 22:50:48 `ok`, заморозка снята, «Расхождение устранено».
- [x] 8.3 Рестарт контура: отпечаток тот же, первой тревоги «счёт не пуст» нет.
  Пройдено 25.11: остановка `no_entries` и запуск через API — карточки «остановлен» и «запущен», отпечаток прежний, тревоги нет, сверка `ok`. Рестарт всего робота владелец сделала дважды при переходе на ветку; «Счёт Финам» тогда ещё не был запущен.
- [x] 8.4 «Счёт» на Т-Инвест и «Песочница» работают как прежде.
  Пройдено 25.11 на тестовом роботе 8102 (код Ф2), проверка через API, логи и базы доставки:
  - **«Песочница».** С 22:43 МСК работает с заявками, 84 привязки. Карточка «ПЕСОЧНИЦА · Контур запущен» доставлена. Сверка `ok`, `status.json` с `broker = tinvest`. Ошибок и предупреждений в логе нет.
  - **«Счёт».** Владелец запустил его в 22:54 МСК, `orders = true`, торговля с заявками поднялась. Карточка «СЧЁТ · Контур запущен» доставлена. Допуск прежний (`full_access`, срочный рынок, тариф, лимиты). Сверка `ok`, разведка комиссий на месте, `status.json` и API с `broker = tinvest`. Ошибок и предупреждений в логе нет.
- [ ] 8.5 Страница «Счёт Финам» в UI: сводка, сверка, лог, метка брокера; кнопки состава недоступны.
