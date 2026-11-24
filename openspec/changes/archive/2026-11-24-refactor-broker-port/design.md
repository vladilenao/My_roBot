## Context

Контур на брокере (`src/broker_contour/`) работает в двух экземплярах: «Песочница» и «Счёт». Оба исполняют команды `TradeManager` настоящими заявками Т-Инвест через `TInvestBroker` и `TInvestApi`. Финам подключается контуром `finam` вида «счёт» (Ф2–Ф3). Т-Инвест не заменяется: контуры на разных брокерах работают одновременно.

Сейчас общий код знает Т-Инвест изнутри:

- рубли за контракт переводит в пункты (`trade_price`, `fill_average`, `_same_stop_price`);
- ищет заявку по ключу, а потерянную стоп-заявку — по атрибутам;
- находит комиссии по операциям Т-Инвест (`FeeSync`);
- включает `GetMaxLots` условием `contour_id == "account"`;
- сам открывает шлюз и читает счёт вызовами SDK.

Факты разведки Финама (Ф0, `add-finam-probe/design.md`, «Факты разведки»), которые порт должен выдержать:

| Тема | Т-Инвест | Финам |
|---|---|---|
| Поиск заявки | по ключу, в любой день | по ключу — только в тот же день, потом по номеру |
| Потерянная стоп-заявка | ключа в ответах нет — по атрибутам | ключ в тот же день, потом по номеру |
| Сработавший стоп | `GetStopOrders` → `exchange_order_id` | `GetOrder` по номеру стопа отдаёт порождённую заявку |
| Цены | у фьючерса суммы в рублях за контракт | везде пункты |
| Временный номер в стриме | есть | нет |
| ГО | `GetFuturesMargin` раз в сутки | своё, плавает, в момент заявки |
| Объём по обеспечению | `GetMaxLots` | нет |
| Комиссия | `executed_commission`, операции | позже в `Transactions` (уточняется 25.11) |
| Авторизация, лимит | токен; подсказка `ratelimit_reset` | JWT 15 мин; 200/мин на метод, без подсказки |

## Goals / Non-Goals

**Goals:**

- Общий код спрашивает брокера намерениями контура («найди заявку», «где мои стопы», «комиссии этих исполнений»), а как это сделать, решает адаптер.
- Общий код не знает SDK и пакеты адаптеров. Адаптер выбирает и передаёт одна точка сборки — `run.py`.
- Логика Т-Инвест не меняется: она переезжает в адаптер дословно.
- Новый экземпляр вида «счёт» добавляется описанием контура, без копии функций.

**Non-Goals:**

- Адаптер Финама, контур `finam`, его API, конфиг и UI — Ф2–Ф3. Веток и свойств «на вырост» под Финам в Ф1 нет.
- Слой API и UI (`account_control`, `comparison_view`, `export_api`) — Ф2, вместе с `/api/v1/finam`.
- Разбор `LiveBroker` (~1000 строк) и примеси `StopKeeping` на части — отдельная задача. Ф1 их не трогает, кроме мест вызова порта.

## Decisions

### 1. Схема

```
 контуры (процессы)                общий код                    порты                  адаптеры
 sandbox ─ data/contours/sandbox/   LiveBroker ─────────► OrderPort  StopPort  ◄──┬── tinvest/  (Ф1: переезд)
           broker.sqlite3, канал    ├ StopKeeping         MarketPort             │     TInvestPort ─► TInvestApi ─► SDK
 account ─ data/contours/account/   └ OrderStream ──────► OrderFeed              │     TInvestSession
           broker.sqlite3, канал    BrokerContour ──────► BrokerSession          │       (счёт, допуск, снимок,
 finam   ─ data/contours/finam/      ├ admission          MarginLimits?          │        комиссии, ГО)
   (Ф2)    broker.sqlite3, канал     ├ reconcile, guards  FeeSource?             └── finam/    (Ф2–Ф3, рядом)
                                     └ status.json        отчёты в пунктах
                    ▲
 run.py — точка сборки: описание контура → адаптер → BrokerContour и LiveBroker
```

У каждого контура свой процесс `run.py --contour <номер>`, свой каталог, `broker.sqlite3`, журнал сделок, отпечаток счёта и канал. Сбой одного не останавливает остальных.

### 2. Порты — `src/broker_contour/port.py`

Порты узкие, по потребителям (ISP). Каждый описывает, что нужно контуру, а не какой вызов есть у брокера.

| Порт | Методы | Кто пользуется |
|---|---|---|
| `OrderPort` | `post_limit`, `post_market`, `cancel`, `order_state(uid, key, number)` | `LiveBroker` |
| `StopPort` | `post_stop`, `cancel_stop`, `poll_stops(queries, known, since, until)` | `StopKeeping` |
| `MarketPort` | `last_price`, `price_band`, `instrument_uid`, `futures_margin` | `LiveBroker` |
| `OrderFeed` | `order_stream()` | `OrderStream` |
| `MarginLimits` | `max_lots(uid, side, price)` | `LiveBroker`, если передан |
| `FeeSource` | `run(now) → [FeeAdjustment]` | `BrokerContour`, если есть |
| `BrokerSession` | `find_account()`, `read_facts(account)`, `read_account(account, names)`, `capital(account)`, `short_name(uid, names)`, `trading(account, book)`, `fee_source(account, journal)`, `fee_probes(directory)` | `BrokerContour`, `admission` |

`BrokerApi = OrderPort + StopPort + MarketPort + OrderFeed` — для удобства сборки. Потребители объявляют только свой узкий порт.

`BrokerSession.trading(account, book)` отдаёт `TradingLink`: порты, `MarginLimits` или `None`, префикс исполнений, период ГО и фабрику `FeeSource` — всё, что адаптер даёт торговле контура. `uid` в `order_state` — инструмент строки журнала: по его карточке адаптер переводит суммы в пункты.

**Намерения вместо вызовов:**

- `order_state(uid, key, number)` — «состояние моей заявки». Т-Инвест: по ключу, если он есть, иначе по номеру биржи — ровно нынешние `order_by_key` и `order_by_exchange_id` и выбор между ними в `_refresh`. Финам выберет сам: в тот же день по ключу, потом по номеру.
- `poll_stops(queries, known, since, until)` — «где мои стоп-заявки». На вход:
  - запросы `StopQuery(key, stop_order_id, instrument_uid, side, lots, stop_price, sent_at)` по живым и «потерянным» строкам, в нынешнем порядке;
  - номера всех известных стоп-заявок (`known`).

  На выход — `StopMatch(report | None, candidates, searched)` на каждый запрос; `StopQuery.search` — номер мог не прийти, `StopQuery.lost` — строка «потеряна», ищется лишняя. Т-Инвест: один `GetStopOrders`, затем поиск по номеру, а без номера — по атрибутам. Сюда дословно переезжают `_candidates`, `_same_stop_price`, `FOUND_SLACK` и правило «одна привязка на стоп-заявку». Машина состояний строки остаётся в общем коде: «неизвестна → потеряна → лишняя снята / не дошла», а также сроки `STOP_LOST` и `STOP_LOST_WINDOW`. Финам по `known` спросит `GetOrder` по номеру.
- `FeeSource.run(now)` — «настоящие комиссии ждущих исполнений». `FeeSync` целиком, вместе с поиском по операциям, долями заглушек и `LIKE 'tinvest:%'`, переезжает в `tinvest/fees.py`. В общем коде остаётся вызов источника на сверке.

**Отчёты порта — в пунктах и лотах заявки:**

- `BrokerTrade(trade_id, price, quantity, at)` — без `in_rub`;
- `OrderReport` — без `executed_rub` и `initial_rub`, но с `average`: средней исполнения в пунктах, если сделок нет. `temporary` остаётся: адаптер без временных номеров всегда ставит `False`;
- `StopReport` — без стоп-цены, она нужна только поиску внутри адаптера.

Перевод рублей в пункты делает адаптер Т-Инвест. Для этого он получает книгу цен `PriceBook`: карточки по номеру инструмента — шаг, его стоимость, лот, признак фьючерса. `PriceBook` выделяется из `LiveBroker` (`_cards`, `_keys`, `_card_by_uid`); брокер её заполняет, адаптер читает. `points_from_rub`, `fill_average`, `trade_price` переезжают в `tinvest/prices.py`.

**Остаётся в общем коде осознанно:** `executed_lots` и `trade_lots` — защита от лотов в штуках (песочница, GAZP 11.11). Решение «штуки или лоты» опирается на журнал заявки (лоты строки и уже записанные исполнения), а адаптер журнала не видит. У брокера, который присылает лоты, проверка ничего не делает.

**Ошибки и общие типы.** В `port.py` переезжают:

- `OrderFailure` и `BrokerCallError` (из `src/sandbox/gateway.py`);
- `AccountRuleError`, `RobotAccount`, `AccountSnapshot`, `mask_account`;
- `StopQuery`, `StopMatch`, `PriceBook`.

### 3. Адаптер Т-Инвест — пакет `src/broker_contour/tinvest/`

Два слоя:

- **`TInvestApi`** (`tinvest/api.py`) — шлюз к SDK, как сейчас. Сырые отчёты Т-Инвест (с `in_rub`, `executed_rub`), разбор ошибок `30057` и `30079`. Методы и вызовы SDK не меняются.
- **`TInvestPort`** (`tinvest/port.py`) — адаптер поверх `TInvestApi`, реализует порты:
  - переводит отчёты в пункты по `PriceBook`;
  - выбирает между поиском по ключу и по номеру;
  - ищет стоп-заявки;
  - реализует `MarginLimits` только для «Счёта».

| Модуль | Откуда переезжает |
|---|---|
| `api.py` | `broker_contour/api.py` |
| `port.py` | поиск стопов из `stops.py`, рублёвые места из `broker.py` |
| `prices.py` | `points_from_rub`, `fill_average`, `trade_price` из `prices.py` |
| `fees.py` | `broker_contour/fees.py` |
| `gateway.py` | `sandbox/gateway.py` |
| `account.py` | `read_account`, `position_conflicts` (`broker_contour/account.py`), `robot_account` (`sandbox/account.py`), `account_by_id` (`account/account.py`) |
| `admission.py` | `account/admission.py` |
| `session.py` | `TInvestSession`: правило счёта, факты, снимок, капитал (`rub_balance`), имена (`short_name_by_uid`), `trading()`, `fee_source()`, `fee_probes()` |
| `__init__.py` | `adapter(profile, …)` — сборка для `sandbox` и `account` |

Реэкспорты остаются только в `src/sandbox/gateway.py` и `src/sandbox/account.py`: на них стоит разведка песочницы (`python -m src.sandbox`). Удалить их можно, когда разведку переведут на новый путь. Остальные старые модули удаляются, импорты в коде и тестах переходят на новые места.

Фейк тестов `FakeApi` — фейк уровня `TInvestApi`. Тесты собирают брокер так: `LiveBroker(TInvestPort(FakeApi(), book), …)`. Поэтому проверки вызовов (`api.calls`, `api.posts()`) и сценарии остаются как есть, а поиск стопов и перевод рублей проверяются в настоящем адаптере, а не в фейке.

### 4. Сборка в одной точке и внедрение зависимостей

- `BrokerContour(settings, session_factory, …)`. `session_factory()` — контекстный менеджер открытого `BrokerSession`. Т-Инвест по умолчанию нет, реестра адаптеров и ленивых импортов нет.
- `LiveBroker(port, ledger, *, book, margin_limits=None, fills_prefix, margin_every, …)`. Объём по обеспечению спрашивается, только если передан `margin_limits`: условие `contour_id == "account"` и флаг `ask_max_lots` уходят.
- `run.py` — точка сборки (composition root). По описанию контура она строит адаптер `tinvest.adapter("sandbox" | "account", …)` и передаёт его в `BrokerContour`. На Ф2 рядом встанет `finam.adapter(…)`.

**Данные брокера, которые остаются параметрами** (не поведение):

| Параметр | Т-Инвест | Зачем |
|---|---|---|
| `fills_prefix` | `tinvest` | номера исполнений `tinvest:<сделка>`, `tinvest-order:<заявка>:<лоты>` |
| `margin_every` | 1 сутки | как часто перечитывать ГО |

Префикс Т-Инвест не меняется. Иначе старые базы «Счёта» и песочницы задвоили бы исполнения: `journal_has` и `_settle` сравнивают номер целиком.

**Тесты меняют сборку объектов, а не проверки.** Конструкторы требуют адаптер явно, поэтому меняются фейки, фикстуры и строки сборки в тестах. Сами проверки (`assert`, ожидаемые вызовы, сценарии) не меняются. Если тесту понадобится новая проверка, она отдельная и названа в tasks.

### 5. `LiveBroker`

`TInvestBroker` переименовывается в `LiveBroker`. Алиаса нет: он закреплял бы имя брокера в общем коде. Имя в архивных change остаётся историей.

### 6. Описание контура — `src/broker_contour/contours.py`

```python
@dataclass(frozen=True)
class LiveContour:
    contour_id: str             # sandbox · account        (Ф2: finam)
    kind: str                   # sandbox · account        (Ф2: account)
    broker: str                 # отпечаток в broker.sqlite3: sandbox · account
    adapter: str                # tinvest-sandbox · tinvest-account
    title: str                  # ПЕСОЧНИЦА · СЧЁТ
    token_env: str              # TINKOFF_SANDBOX_TOKEN · TINKOFF_TRADE_TOKEN
    account_id_env: str | None  # — · TINKOFF_ACCOUNT_ID
```

`LIVE_CONTOURS` — «Счёт», затем песочница: это порядок подъёма. Описание — данные, без импортов адаптеров.

По описанию работают:

- **`run.py`:** одна функция запуска контура на брокере и надзиратели циклом по описаниям;
- **`BrokerContourSupervisor(contour_id, kind)`:** пишет `launch.json`;
- **`status.json`:** `kind` берётся из описания;
- **настройки:** `[contours.<номер>]` читаются по номеру;
- **заголовки карточек:** `CONTOUR_TITLES` в `notify.py` и `telegram_templates.py` строятся из описаний.

У `sandbox` и `account` значения прежние: файлы, пути, ключи, события и тексты не меняются.

### 7. Три шага, каждый — коммит с зелёным `pytest`

| Шаг | Что | Логика |
|---|---|---|
| 1. Переносы | пакет `tinvest/` (`api`, `gateway`, `account`, `admission`); общие типы в `port.py`; реэкспорты в `src/sandbox` | не меняется ни одна строка тел функций |
| 2. Порты | `TInvestPort`, `PriceBook`, `TInvestSession`, `FeeSource`; `LiveBroker` и `BrokerContour` на портах; граница «общий код без SDK и адаптеров» | код Т-Инвест переезжает дословно, меняется место вызова |
| 3. Описание контура | `LiveContour`, `run.py`, надзиратели, `status.json`, заголовки | значения у двух контуров прежние |

Граница проверяется тестом на шаге 2. Модули `broker`, `stops`, `stream`, `process`, `admission`, `prices`, `orders`, `reconcile`, `guards`, `control`, `notify`, `fingerprint`, `store`, `bindings`, `port`, `contours` не импортируют (разбором AST, в том числе внутри функций):

- `t_tech`, `grpc`;
- `src.sandbox`;
- `src.broker_contour.tinvest`, `src.broker_contour.finam`.

### 8. Контрактные тесты порта

Общий набор `tests/unit/broker_contour/contract/`, параметризованный адаптером. Сценарии:

- вход и исполнение;
- цель;
- стоп у брокера и его срабатывание;
- потерянный ответ на постановку заявки и стопа;
- рестарт с досверкой;
- снятие.

В Ф1 набор гоняется на `TInvestPort(FakeApi)`. На Ф3 тот же набор — на фейке Финама: так проверяется, что адаптер Финама выполняет тот же контракт.

### 9. Как встанет Финам (Ф2–Ф3)

| Порт | Финам |
|---|---|
| `BrokerSession` | шлюз с обновлением JWT по `SubscribeJwtRenewal` (стримы не переоткрываются); правило «один счёт токена = `FINAM_ACCOUNT_ID`»; снимок из `GetAccount` без нулевых позиций; капитал из `cash` со строками вариационной маржи |
| `OrderPort` | `PlaceOrder`; `client_order_id` — 20 знаков из ключа журнала, детерминированно; `order_state`: тот же день — `GetOrders` по ключу, иначе `GetOrder` по номеру; рыночную заявку Финам сделает IOC-лимитом — частичное исполнение и снятие общий код уже разбирает |
| `StopPort` | `ORDER_TYPE_STOP`, `VALID_BEFORE_GOOD_TILL_CANCEL`, `DAY`; `poll_stops`: `GetOrders` плюс `GetOrder` по `known`; другой номер в ответе — стоп исполнен |
| `MarketPort` | `LastQuote`, ГО из `GetAssetParams` без стоимости шага; `margin_every` — по итогам Ф3 |
| `OrderFeed` | слияние `SubscribeOrders` и `SubscribeTrades`, пустое сообщение подписки — `None` |
| `MarginLimits` | не передаётся |
| `FeeSource` | `Transactions` или тариф — по итогам 25.11 |
| лимит 200/мин | повтор в шлюзе ждёт `reset_time` из `GetUsageMetrics`; исчерпан — `OrderFailure("rate_limited")` |

Общий код на Ф2–Ф3 не меняется. Добавляются только пакет `finam/`, описание контура и строка сборки в `run.py`.

### Отклонено

- **Флаги поведения в общем коде** (`order_lookup`, `stop_lookup`, `rub_amounts`): общий код ветвился бы по брокеру, и каждый новый брокер добавлял бы ветки. Нарушает открытый-закрытый принцип.
- **Один широкий `BrokerApi` с необязательными методами:** у Финама был бы метод `max_lots`, который нельзя вызывать. Это нарушение LSP.
- **Реестр адаптеров с ленивыми импортами и Т-Инвест по умолчанию:** скрытые зависимости (Service Locator).
- **Алиас `TInvestBroker` и правило «тесты меняют только импорты»:** закрепляли бы Т-Инвест в общем коде ради неизменных тестов.

## Risks / Trade-offs

- **Ошибка переноса в коде реальных денег.** Три шага с зелёным `pytest` после каждого; переносы дословные; проверки тестов не меняются. Плюс тест на старую базу и ручной регресс в песочнице и на «Счёте» только на чтение — до выкладки на ПРОМ.
- **`average` в адаптере.** Сейчас `_refresh` считает среднюю по лотам после перевода штук в лоты по журналу (`row.lots`). Адаптер журнала не видит и переводит по `lots_requested` брокера. У Т-Инвест эти числа совпадают. Тест с GAZP (лот 10, 100 штук) проверяет одинаковый результат.
- **Поиск стопов пачкой.** `poll_stops` получает все запросы за один опрос, в прежнем порядке «живые → потерянные», и ведёт один набор привязанных номеров — как нынешний цикл. Тесты `test_broker_stops` это покрывают.
- **Объём изменений больше, чем у варианта с флагами:** поиск стопов, рублёвый перевод и `FeeSync` переезжают. Зато Ф3 не трогает общий код.
- **PyInstaller** может не увидеть новый пакет. Проверка — сборка `run.spec` и запуск собранного `--contour sandbox` до допуска.

## Migration Plan

Данных не переносит: базы, файлы, ключи и пути прежние. Выкладка — обычным релизом после ручного регресса. Откат — предыдущий релиз: схема баз не меняется.

## Open Questions

Нет: источник комиссий и частота ГО Финама решаются на Ф3, и Ф1 не оставляет под них ни веток, ни флагов.
