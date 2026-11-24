## Context

Контур на брокере (`src/broker_contour/`) работает в двух экземплярах: «Песочница» и «Счёт». Оба исполняют команды `TradeManager` настоящими заявками Т-Инвест через `TInvestBroker` и адаптер `TInvestApi`. Финам подключается третьим экземпляром — контуром `finam` вида «счёт» (Ф2–Ф3). Этот change — Ф1: чистый рефакторинг, после которого адаптер Финама встаёт рядом с адаптером Т-Инвест и не трогает общий код.

Факты разведки Финама (Ф0, `feature/finam-probe`, `add-finam-probe/design.md`, раздел «Факты разведки»), важные для порта:

| Тема | Т-Инвест | Финам |
|---|---|---|
| Ключ заявки | UUID ≤ 36, ищется по ключу и на другой день | `client_order_id` ≤ 20, надёжен только в тот же день |
| Поиск после рестарта | `GetOrderState` по ключу | в тот же день — по ключу, на другой — по номеру |
| Стоп-заявка в ответах чтения | без ключа, ищется по атрибутам | ключ в тот же день, потом пусто |
| Сработавший стоп | `GetStopOrders` → `exchange_order_id` | `GetOrder` по номеру стопа отдаёт порождённую заявку |
| Цены | у фьючерса суммы в рублях за контракт | везде пункты |
| Временный номер в стриме | есть | нет |
| ГО | `GetFuturesMargin`, раз в сутки | своё, плавает, берётся в момент заявки |
| Объём по обеспечению | `GetMaxLots` | нет |
| Рыночная заявка | рыночная | IOC-лимит по границе планки |
| Комиссия | `executed_commission`, операции | в сделке нет, позже в `Transactions` (уточняется 25.11) |
| Авторизация | токен | JWT на 15 мин, `SubscribeJwtRenewal` |
| Лимит | подсказка `ratelimit_reset` | 200 в минуту на метод, подсказки нет |

## Goals / Non-Goals

**Goals:**

- Общий код контура не знает SDK брокера: только порт.
- Особенности брокера — свойства адаптера.
- Новый экземпляр вида «счёт» добавляется данными: описанием контура.
- Нулевое изменение поведения «Счёта» и песочницы.

**Non-Goals:**

- Контур `finam`, адаптер Финама, его API, конфиг и UI — Ф2–Ф3.
- Параметризация слоя API и UI (`account_control`, `comparison_view`, `export_api`) — Ф2, вместе с `/api/v1/finam`.
- Перенос разведки комиссий «Счёта» (`src/account/fee_probes.py`) — она живёт только при `orders = false` и остаётся на месте.

## Decisions

### 1. Схема

Оба брокера нужны одновременно, Т-Инвест не заменяется. Контуры вида «счёт» с разными брокерами работают рядом. У каждого свой процесс, каталог, база, отпечаток счёта и канал. Код Т-Инвест в Ф1 только переезжает за порт.

```
 контуры (процессы)          общий код                порт            адаптеры (пакеты)
 ─────────────────          ─────────                ────            ─────────────────
 sandbox ─┐                                                       ┌─► tinvest/   Т-Инвест
 data/contours/sandbox/     LiveBroker               BrokerApi    │   api, gateway, account,
 broker.sqlite3, канал ПЕСОЧН.  stops, fees, stream ─► BrokerAccess ─┤   admission, access
          ├─► LiveContour ─► BrokerContour       ─► BrokerTraits  │   (Ф1: переезд без правок
 account ─┤   (описание)    admission, reconcile,                 │    логики)
 data/contours/account/     guards, control                      │
 broker.sqlite3, канал СЧЁТ                                        └─► finam/     Финам
          │                 не знает ни tinvest/,                    api, gateway (JWT),
 finam ───┘ (Ф2)            ни finam/ — только порт                  account, access
 data/contours/finam/                                                (Ф2–Ф3, рядом)
 broker.sqlite3, канал ФИНАМ

 profiles.py (сборка): профиль описания → адаптер. tinvest-sandbox, tinvest-account; Ф2: finam-account
```

- **Контур** — процесс `run.py --contour <номер>` со своим каталогом, базой `broker.sqlite3`, журналом сделок, отпечатком счёта и каналом. Процессы друг друга не ждут, сбой одного не останавливает остальных.
- **Общий код** работает только с портом. Ни `tinvest/`, ни `finam/` он не импортирует.
- **Адаптер выбирает сборка.** Реестр `src/broker_contour/profiles.py` по профилю описания (решение 6) отдаёт `BrokerAccess` и `BrokerTraits`. Только он знает пакеты адаптеров и импортирует их лениво.

### 2. Порт — `src/broker_contour/port.py`

**`BrokerApi`** — `typing.Protocol` с методами нынешнего `TInvestApi`:

| Группа | Методы |
|---|---|
| Заявки | `post_limit`, `post_market`, `cancel`, `order_by_key`, `order_by_exchange_id`, `max_lots` |
| Стоп-заявки | `post_stop`, `cancel_stop`, `stop_orders(since, until, ids=())` |
| Счёт | `operations`, `futures_margin` |
| Рынок | `last_price`, `price_band`, `instrument_uid` |
| Стрим | `order_stream` |

`instrument_uid` тоже в порте: `LiveBroker._card_for` переводит тикер эмуляции в номер инструмента брокера.

Контракт порта не требует того, чего нет у Финама:

- **Поиск по ключу** обязан работать только в тот же день. Дальше заявку ищут по номеру — решение 4, свойство `order_lookup`.
- **`max_lots`** вызывается, только если `traits.max_lots`.
- **Комиссия** в `OrderReport.executed_fee` и в операциях необязательна: нет её — менеджер ставит оценку (`fee_source = configured`), как сейчас.
- **`price_band`** может вернуть `None`: коридор тогда проверяет биржа.
- **`futures_margin`** может не вернуть стоимость шага: тогда остаётся шаг ядра.
- **`stop_orders`** получает номера известных контуру стоп-заявок (`ids`). Т-Инвест их не использует. Финаму они нужны: на другой день `GetOrders` не показывает исполненные и снятые, а `GetOrder` по номеру — показывает.

**`BrokerAccess`** — доступ к счёту на открытом шлюзе:

- `find_account()` — правило счёта;
- `read_facts(account)` — факты допуска;
- `read_account(account, names)` — снимок `AccountSnapshot`;
- `capital(account)` — рубли счёта;
- `short_name(uid, names)` — короткое имя инструмента;
- `api(account)` — `BrokerApi` на этом счёте.

Сбой брокера — `BrokerCallError`, как сейчас.

В порт переезжают общие типы:

- отчёты `BrokerTrade`, `OrderReport`, `StopReport`, `OperationReport`;
- ошибки `OrderFailure`, `BrokerCallError` (из `src/sandbox/gateway.py`), `AccountRuleError`;
- счёт и снимок `RobotAccount`, `AccountSnapshot`, `mask_account`.

`StopReport` получает необязательное поле `key` (ключ стоп-заявки, если брокер его отдаёт). Т-Инвест оставляет `None`.

### 3. Адаптер Т-Инвест — пакет `src/broker_contour/tinvest/`

| Модуль | Откуда |
|---|---|
| `api.py` | `TInvestApi`, `classify`, `report_from_state/stream` из `broker_contour/api.py` |
| `gateway.py` | `BrokerGateway`, `SandboxGateway`, `AccountGateway` из `sandbox/gateway.py` |
| `account.py` | `read_account`, `position_conflicts` из `broker_contour/account.py`; `robot_account` из `sandbox/account.py`; `account_by_id` из `account/account.py` |
| `admission.py` | `read_facts`, `_limits` из `account/admission.py` |
| `access.py` | `TInvestAccess` и профили `SANDBOX`, `ACCOUNT`: шлюз, правило счёта, факты, свойства |

Реэкспорты:

- `src/sandbox/gateway.py` и `src/sandbox/account.py` — для разведки песочницы (`python -m src.sandbox`);
- `src/broker_contour/admission.py` по-прежнему отдаёт `admit_account`, `check_settings`, `AdmissionError`; правило счёта в `admit_account` теперь обязательное.

Модули `src/broker_contour/api.py`, `src/broker_contour/account.py`, `src/account/account.py`, `src/account/admission.py` удаляются. Их импорты в коде и тестах переходят на `port` и `tinvest`.

**Граница проверяется тестом:** модули `broker`, `stops`, `fees`, `stream`, `process`, `admission`, `prices`, `orders`, `reconcile`, `guards`, `control`, `notify`, `fingerprint`, `store`, `bindings`, `port`, `contours` не импортируют:

- `t_tech`, `grpc`;
- `src.sandbox`;
- пакеты адаптеров `src.broker_contour.tinvest` и `src.broker_contour.finam`.

Проверяются и импорты внутри функций — разбором AST. Пакеты адаптеров знает только реестр `profiles.py`. `BrokerContour` берёт доступ у реестра по `settings.profile`, а прежние параметры конструктора (`gateway_factory`, `find_account`, `read_facts`) остаются переопределениями для тестов.

### 4. Свойства брокера — `BrokerTraits`

| Свойство | Где сейчас | Т-Инвест «Песочница» | Т-Инвест «Счёт» | Финам (Ф3, ожидаемо) |
|---|---|---|---|---|
| `fills_prefix` — префикс исполнений | `broker.py:50`, `fees.py:33`, `removeprefix` в командах целей и стопа | `tinvest` | `tinvest` | `finam` |
| `rub_amounts` — суммы и стоп-цена бывают в рублях за контракт | `stops._same_stop_price` | да | да | нет |
| `max_lots` — спрашивать объём по обеспечению | `process.py:303` (`contour_id == "account"`) | нет | да | нет |
| `margin_every` — как часто читать ГО брокера | `broker.MARGIN_EVERY` | сутки | сутки | короче — решит Ф3 |
| `order_lookup` — как досверять заявку | `broker._refresh`: всегда по ключу | `key` | `key` | `number` |
| `stop_lookup` — как искать стоп-заявку с потерянным ответом | `stops._candidates`: по атрибутам | `attributes` | `attributes` | `key` |
| `fees` — откуда настоящие комиссии | `process.py` всегда `FeeSync` | `operations` | `operations` | решит Ф3 |

Из префикса выводятся два вида номеров исполнений: `tinvest:<сделка биржи>` и `tinvest-order:<заявка>:<лоты>`. Префикс Т-Инвест не меняется. Иначе старые базы «Счёта» и песочницы задвоили бы исполнения: `journal_has` и `_settle` сравнивают номер строки целиком, а `FeeSync` ищет ждущие исполнения по `LIKE 'tinvest:%'`.

Временный номер стрима свойством не нужен: это уже поле отчёта `OrderReport.temporary`. Адаптер без временных номеров всегда ставит `False`, общий код ветки не меняет.

`order_lookup = number`: досверка заявки с известным номером идёт через `order_by_exchange_id`, без номера — по ключу. `stop_lookup = key`: потерянная стоп-заявка ищется по `StopReport.key`, а не по атрибутам. Обе ветки на Ф1 покрываются контрактными тестами на фейке. Т-Инвест идёт прежним путём.

`LiveBroker(api, ledger, *, traits=TINVEST, ask_max_lots=None, …)`. `ask_max_lots=None` берёт `traits.max_lots`; явное значение остаётся для тестов. Без `traits` — свойства Т-Инвест, поэтому прежние вызовы ведут себя как сейчас.

### 5. `LiveBroker`

`TInvestBroker` переименовывается в `LiveBroker`. `TInvestBroker = LiveBroker` остаётся алиасом в `broker.py`: на него ссылаются тесты и документы архивных change. Примесь `StopKeeping` и `FeeSync` получают свойства от брокера.

### 6. Описание контура на брокере — `src/broker_contour/contours.py`

```python
@dataclass(frozen=True)
class LiveContour:
    contour_id: str        # sandbox · account        (Ф2: finam)
    kind: str              # sandbox · account        (Ф2: account)
    broker: str            # отпечаток в broker.sqlite3: sandbox · account
    profile: str           # адаптер: tinvest-sandbox · tinvest-account
    title: str             # ПЕСОЧНИЦА · СЧЁТ
    token_env: str         # TINKOFF_SANDBOX_TOKEN · TINKOFF_TRADE_TOKEN
    account_id_env: str | None   # — · TINKOFF_ACCOUNT_ID
```

`LIVE_CONTOURS` — кортеж из двух описаний. Порядок задаёт порядок подъёма: «Счёт» первым.

По описанию работают:

- **`run.py`.** `_run_broker_contour(contour_id)` собирает настройки из `[contours.<id>]` общим чтением ключей `contours_<id>_<ключ>`, а шлюз, правило счёта и факты — из профиля. `_run_account_contour` и её копия для песочницы сливаются в одну функцию. Надзиратели строятся циклом по `LIVE_CONTOURS`.
- **`BrokerContourSupervisor(contour_id, kind)`.** `launch.json` пишет оба поля, как сейчас.
- **`BrokerContourSettings`** получает `kind`. В `status.json` поле `kind` берётся из него, а не из `contour_id`; значения у двух контуров прежние.
- **Заголовок уведомлений** (`CONTOUR_TITLES` в `notify.py` и `telegram_templates.py`) берётся из описания.

Значения у `sandbox` и `account` прежние, поэтому файлы, пути, ключи, события и тексты не меняются.

### 7. Как на порт встанет Финам

**Ф2 — контур `finam`, только чтение:**

- описание `LiveContour("finam", "account", "finam", "finam-account", …)`;
- `src/broker_contour/finam/access.py`: шлюз Финама с обновлением JWT по `SubscribeJwtRenewal` (стримы не переоткрываются), правило счёта «один счёт токена = `FINAM_ACCOUNT_ID`», снимок из `GetAccount` без нулевых позиций, капитал из `cash` с учётом строк вариационной маржи;
- API `/api/v1/finam`, `[contours.finam]` и страница «Счёт» с параметром контура.

**Ф3 — заявки и стопы:** `src/broker_contour/finam/api.py`, реализация `BrokerApi`.

| Метод порта | Финам |
|---|---|
| ключ заявки | `client_order_id` — 20 знаков из ключа журнала (`order_key`), детерминированно; журнал хранит прежний ключ |
| `post_limit`, `post_market` | `PlaceOrder`; рыночная станет IOC-лимитом по границе — общий код уже разбирает частичное исполнение и снятие |
| `order_by_key` | `GetOrders` по `client_order_id`, только тот же день |
| `order_by_exchange_id` | `GetOrder` по номеру |
| `post_stop`, `cancel_stop` | `PlaceOrder` `ORDER_TYPE_STOP`, `VALID_BEFORE_GOOD_TILL_CANCEL`, `DAY` |
| `stop_orders(…, ids)` | `GetOrders` плюс `GetOrder` по каждому номеру из `ids`; другой номер в ответе — стоп исполнен, `exchange_order_id` — номер порождённой заявки |
| `operations` | `Trades` и `Transactions`; источник комиссий — по итогам 25.11 |
| `futures_margin` | ГО из `GetAssetParams`, без стоимости шага |
| `max_lots` | не вызывается (`max_lots = False`) |
| `order_stream` | слияние `SubscribeOrders` и `SubscribeTrades`; пустое сообщение подписки — `None` |
| лимит 200/мин | повтор в шлюзе ждёт `reset_time` из `GetUsageMetrics`; исчерпан — `OrderFailure("rate_limited")` |

Свойства Финама: `fills_prefix = finam`, `rub_amounts = False`, `max_lots = False`, `order_lookup = number`, `stop_lookup = key`, `margin_every` и `fees` — по итогам Ф3.

**Ф4** (предохранители, сравнение, перенос привязок) общий код не меняет: предохранители и сверка работают через порт.

## Risks / Trade-offs

- **Ошибка рефакторинга в коде реальных денег.**
  - Переносы механические, логика методов не меняется.
  - Существующие тесты не меняют проверок.
  - Отдельный тест на старую базу с префиксом `tinvest:`.
  - Ручной регресс в песочнице и на «Счёте» только на чтение — до выкладки на ПРОМ.
- **Свойства с ветками, которые Т-Инвест не использует** (`order_lookup = number`, `stop_lookup = key`) — код без живой проверки до Ф3. Его покрывают контрактные тесты на фейке; ПРОМ его не исполняет.
- **Сборка PyInstaller** может не увидеть новый пакет при ленивых импортах. Проверка — сборка `run.spec` и запуск `--contour sandbox` из сборки на тестовых данных.
- **Реэкспорты в `src/sandbox`** оставляют два пути к шлюзу. Убрать их можно позже, вместе с разведкой песочницы.

## Migration Plan

Данных не переносит: базы, файлы, ключи и пути прежние. Выкладка — обычным релизом после ручного регресса. Откат — предыдущий релиз: схема баз не меняется.

## Open Questions

- Источник комиссий Финама (`fees`) и частота ГО (`margin_every`) — по итогам разведки 25.11 и Ф3. Ф1 оставляет место, значения у Т-Инвест прежние.
