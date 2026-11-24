Ветка `feature/finam-contour` от `origin/release/release-12`, без upstream. Push и PR делает владелец. Существующие тесты проверок не меняют. Баги проверки чинятся в этой ветке и записываются сюда.

## 0. Согласование

- [ ] 0.1 Владелец согласовал proposal и design. Нужны ответы на open questions: код Ф0, имена переменных Telegram, справочник имён на `TINKOFF_TOKEN`.

## 1. Код Ф0

- [ ] 1.1 `git merge --no-ff feature/finam-probe`; полный `pytest` зелёный на слиянии.
- [ ] 1.2 Тест границы общего кода дополнен `src.finam`.

## 2. Адаптер `src/broker_contour/finam/`

- [ ] 2.1 `api.py` — `FinamApi`: вызовы с `retry=False`, повтор по `reset_time` из `GetUsageMetrics` (до 2 раз, не дольше 65 с), окно 05:00–06:15 МСК → `PLANNED_OUTAGE`. Константа `PLANNED_OUTAGE` в `port.py`.
- [ ] 2.2 `jwt.py` — `JwtRenewal`: стрим `SubscribeJwtRenewal`, `adopt_jwt` до использования, переподписка 5/10/30/60 с, закрытие с сессией.
- [ ] 2.3 `symbols.py` — `FinamSymbols`: `symbol` по карточке Т-Инвест (RTSX/MISX), обратное имя и UID, проверка шага и лота (`trade_lot_size`), `margin(symbol)` без кэша.
- [ ] 2.4 `account.py`: правило счёта по `TokenDetails` (номера — секреты до проверки); снимок из `GetAccount` + `GetOrders` (сумма RUB в `cash`, нулевые позиции, лоты акций, активные заявки и стопы); `capital`.
- [ ] 2.5 `admission.py` — факты в схеме «Счёта»: права по `readonly`, квоты, предупреждения.
- [ ] 2.6 `session.py` — `FinamSession` (`BrokerSession`), `trading()` отказывает (Ф3); `adapter(token, account_id, reference_token, …)`.
- [ ] 2.7 Тесты `tests/unit/broker_contour/finam/` на фейковом API Финама: всё из 2.1–2.6.

## 3. Общий код и сборка

- [ ] 3.1 `PLANNED_OUTAGE` в `process.py` и `reconcile.py`: допуск ждёт окна без тревоги, сверка пропускается без счётчика, `account_link: maintenance`. Тесты, включая «Счёт» без изменений.
- [ ] 3.2 `contours.py`: описание `finam`, порядок `account`, `finam`, `sandbox`; `broker` в `status.json`.
- [ ] 3.3 Конфиг: `[contours.finam]` в `config_loader.py`, `config.py`, `default.toml`; `orders = true` → `ConfigError`. Тесты конфига.
- [ ] 3.4 `run.py`: `_broker_sessions` для `finam-account`; `_SECRETS` += `FINAM_SECRET_TOKEN`, `FINAM_ACCOUNT_ID`, токен бота Финама; надзиратель из цикла описаний.
- [ ] 3.5 `console_view.py` — метка из `CONTOUR_TITLES`.
- [ ] 3.6 `run.spec`: `src.finam.proto.*` и `src.broker_contour.finam` в сборке.

## 4. API

- [ ] 4.1 `control_views.py`: пути `/api/v1/finam/*` от базового пути.
- [ ] 4.2 `account_control.py`: `AccountControl` с параметрами экземпляра (контур, база, заголовок, секция конфига); `fee_probes` только у Т-Инвест; `broker` в сводке и `GET`.
- [ ] 4.3 `comparison_view.py`: `AccountComparison` получает имя своей стороны; `SIDES` от него. Путь `account` прежний.
- [ ] 4.4 `export_api.py`: таблица «базовый путь → ресурс» для `account` и `finam`; `GET /api/v1` с ключом `finam`; `/api/v1/finam/{positions,trades,performance,account}`.
- [ ] 4.5 `run.py`: ресурс `finam` и торговля журнала `finam`.
- [ ] 4.6 `docs/api/openapi.yaml` 2.14.0: тег «Счёт Финам», пути, `broker`, `account_link: maintenance`. Тест схемы.
- [ ] 4.7 Тесты API: чтения, `409 ACCOUNT_READ_ONLY`, `404` сравнения и предохранителей, ключ `finam`, `broker`, пути «Счёта» прежние.

## 5. Регресс — автотесты

- [ ] 5.1 Маскирование: `FINAM_SECRET_TOKEN`, `FINAM_ACCOUNT_ID`, каждый JWT и номера счетов `TokenDetails` — нет в логе, `status.json`, событиях канала, ответах API.
- [ ] 5.2 Два живых контура одновременно: `account` и `finam` — свои каталоги, базы, отпечатки; отпечаток `finam` не принимает чужой счёт.
- [ ] 5.3 Конфиг `[contours.finam]`.
- [ ] 5.4 Полный `pytest` и `ruff check src tests run.py`.
- [ ] 5.5 PyInstaller: `run.spec` собирает; собранный `--contour finam` без секрета отказывает «Не задан FINAM_SECRET_TOKEN» (заглушки Финама импортируются).

## 6. UI (репозиторий `UI`, ветка `feature/ui-finam-contour`)

- [ ] 6.1 `TradingContour`, `BrokerContour` += `'finam'`; таблица «контур → базовый путь» в `queries.ts`.
- [ ] 6.2 `AccountPage` с параметром `contour`, маршрут `/finam`, пункт меню «Счёт Финам» при ключе `finam` в `GET /api/v1`.
- [ ] 6.3 Карточка в «Обзоре», строка в «Аварийной остановке», метка брокера в шапке.
- [ ] 6.4 Типы из OpenAPI 2.14.0, `npm run build`, проверка на тестовом роботе 8102.

## 7. Ручная проверка владельца — тестовый робот 8102, не ПРОМ

- [ ] 7.1 Контур `finam` стартует и проходит допуск на реальном счёте Финама. Карточка старта в канале Финама с меткой «СЧЁТ ФИНАМ», последние 4 символа счёта.
- [ ] 7.2 Сверка «ок». Расхождение видно: ручная заявка владельца в терминале Финама → тревога сверки; снята → сверка снова «ок».
- [ ] 7.3 Рестарт контура: отпечаток тот же, первой тревоги «счёт не пуст» нет.
- [ ] 7.4 «Счёт» на Т-Инвест и «Песочница» работают как прежде.
- [ ] 7.5 Страница «Счёт Финам» в UI: сводка, сверка, лог, метка брокера; кнопки состава недоступны.
