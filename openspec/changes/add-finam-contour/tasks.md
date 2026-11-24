Ветка `feature/finam-contour` от `origin/release/release-12`, без upstream. Push и PR делает владелец. Каждый шаг — коммит с зелёным полным `pytest` и чистым `ruff check src tests run.py`. Существующие тесты проверок не меняют. Баги проверки чинятся в этой ветке и записываются сюда.

## 0. Согласование

- [ ] 0.1 Владелец согласовал proposal и design. Нужны ответы на open questions: имена переменных Telegram и каталог на `TINKOFF_TOKEN`.

## 1. Заглушки Ф0

- [ ] 1.1 `git merge --no-ff feature/finam-probe`. Полный `pytest` на слиянии.

## 2. Общий код без веток по брокеру — поведение «Счёта» и песочницы не меняется

- [ ] 2.1 `port.py`:
  - `BrokerOutage(BrokerCallError)`;
  - `BrokerSession` (чтение) и `TradingSession` (капитал, имена, торговля);
  - `InstrumentCard`, `InstrumentCatalog`.
- [ ] 2.2 `process.py`:
  - `_read(session, account)` с исходами «снимок / потерян / окно» вместо трёх перехватов в `_reconcile`, `_reconcile_trading`, `_final_account`;
  - допуск ждёт окна одним циклом.

  `reconcile.py`: пропуск в окне не считается. `admission.py`: `BrokerOutage` не превращается в отказ.
- [ ] 2.3 `contours.py`: поля `provider`, `trading`, `defaults`; `broker` в `status.json`.
- [ ] 2.4 Конфиг: ключи и проверки `[contours.<номер>]` контуров на брокере — по описаниям; `orders = true` при `trading = False` → `ConfigError`.
- [ ] 2.5 `run.py`:
  - `_SECRETS` — из описаний;
  - сборка контура с `orders = true` только при `trading`.

  `console_view.py` — метка из `CONTOUR_TITLES`.
- [ ] 2.6 Тесты 2.1–2.5:
  - `BrokerOutage` при допуске, сверке, остановке, остановка во время ожидания окна;
  - «Счёт» без `BrokerOutage` — прежние тревоги;
  - `ConfigError` для `orders` у описания без торговли;
  - значения `[contours.account]`/`[contours.sandbox]` прежние;
  - граница общего кода дополнена `src.finam`.

## 3. Каталог карточек ядра

- [ ] 3.1 `tinvest/catalog.py` — `TInvestCatalog`: шлюз на `TINKOFF_TOKEN` только `instruments.*`, `by_uid`, `by_ticker`, короткое имя правилом `short_name_by_uid`, кэш на процесс.
- [ ] 3.2 Тесты: карточки акции и фьючерса, отказ не-`instruments.*` до сети.

## 4. Адаптер `src/broker_contour/finam/`

- [ ] 4.1 `transport.py`:
  - `FinamTransport`: канал, `call`, текст ошибки в одну строку;
  - повтор по `reset_time` (до 2, не дольше 65 с, без квоты — 60 с);
  - окно 05:00–06:15 МСК → `BrokerOutage`.
- [ ] 4.2 `transport.py` — `JwtSource`: `Auth` при входе, стрим `SubscribeJwtRenewal`, секрет до использования, замок, переподписка 5/10/30/60 с, запасной `Auth` старше 10 мин; `AuthService` без заголовка.
- [ ] 4.3 `symbols.py` — `FinamSymbols`: `symbol` по карточке (RTSX/MISX), карточка по `symbol`, `SymbolMismatch` по шагу и лоту, `margin(symbol)` без кэша.
- [ ] 4.4 `account.py`:
  - правило счёта по `TokenDetails` (номера — секреты до проверки);
  - снимок: сумма RUB, нулевые позиции, лоты акций, активные заявки и стопы, `names` только по UID.
- [ ] 4.5 `admission.py` — факты `access_level`, `limits`, `warnings`.
- [ ] 4.6 `session.py` — `FinamSession` (`BrokerSession`), `adapter(secret, account_id, catalog_factory)`; `run.py` — `_broker_sessions` для `finam-account`.
- [ ] 4.7 Описание `finam` в `contours.py`; `[contours.finam]` в `default.toml`; `run.spec` — `src.finam.proto.*` и `src.broker_contour.finam`.
- [ ] 4.8 Тесты `tests/unit/broker_contour/finam/` на своём фейке Финама:
  - всё из 4.1–4.6;
  - граница адаптера: без разведки `src.finam`, `tinvest`, `src.sandbox`, `t_tech`.

## 5. API

- [ ] 5.1 `AccountControl` — параметры экземпляра: контур, база, заголовок, провайдер, `orders_enabled`, `reconcile_extras`, сравнение; `routes()`; `broker` в сводке и `GET`.
- [ ] 5.2 `comparison_view.py`: `AccountComparison` с именем своей стороны, `SIDES` от него.
- [ ] 5.3 `export_api.py`:
  - таблица `LIVE` «базовый путь → ресурс» вместо `_account_get` / `_account_command` по `views.ACCOUNT`;
  - `GET /api/v1` по таблице;
  - ресурсы торговли `finam`.
- [ ] 5.4 `run.py`: ресурсы `account` и `finam` с их частями; журнал торговли `finam`.
- [ ] 5.5 `docs/api/openapi.yaml` 2.14.0:
  - тег «Счёт Финам», пути;
  - `Finam`, `FinamCurrent`, `FinamAdmission`;
  - `broker` у `Account`;
  - `account_link: maintenance`.

  Тест схемы.
- [ ] 5.6 Тесты API: чтения `finam`, `409 ACCOUNT_READ_ONLY`, `404` сравнения и предохранителей, ключ `finam`, `broker`; ответы `account` и `sandbox` прежние.

## 6. Регресс — автотесты

- [ ] 6.1 Маскирование: `FINAM_SECRET_TOKEN`, `FINAM_ACCOUNT_ID`, каждый JWT и номера счетов `TokenDetails` — нет в логе, `status.json`, событиях канала, ответах API.
- [ ] 6.2 Два живых контура одновременно: `account` и `finam` — свои каталоги, базы, отпечатки; отпечаток `finam` не принимает чужой счёт.
- [ ] 6.3 Конфиг `[contours.finam]`.
- [ ] 6.4 Полный `pytest` и `ruff check src tests run.py`.
- [ ] 6.5 PyInstaller: `run.spec` собирает; собранный `--contour finam` без секрета отказывает «Не задан FINAM_SECRET_TOKEN».

## 7. UI (репозиторий `UI`, ветка `feature/ui-finam-contour`)

- [ ] 7.1 `TradingContour`, `BrokerContour` += `'finam'`; таблица `CONTOUR_BASE` в `queries.ts`.
- [ ] 7.2 `AccountPage` с параметром `contour`, маршрут `/finam`, пункт меню «Счёт Финам» при ключе `finam` в `GET /api/v1`; блок допуска показывает общие поля и поля своего брокера, если они есть.
- [ ] 7.3 Карточка в «Обзоре», строка в «Аварийной остановке», метка брокера в шапке.
- [ ] 7.4 Типы из OpenAPI 2.14.0, `npm run build`, проверка на тестовом роботе 8102.

## 8. Ручная проверка владельца — тестовый робот 8102, не ПРОМ

- [ ] 8.1 Контур `finam` стартует и проходит допуск на реальном счёте Финама. Карточка старта в канале Финама с меткой «СЧЁТ ФИНАМ» и последними 4 символами счёта.
- [ ] 8.2 Сверка «ок». Расхождение видно: ручная заявка владельца в терминале Финама → тревога сверки; заявка снята → сверка снова «ок».
- [ ] 8.3 Рестарт контура: отпечаток тот же, первой тревоги «счёт не пуст» нет.
- [ ] 8.4 «Счёт» на Т-Инвест и «Песочница» работают как прежде.
- [ ] 8.5 Страница «Счёт Финам» в UI: сводка, сверка, лог, метка брокера; кнопки состава недоступны.
