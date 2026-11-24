## Context

- Ф1 (`refactor-broker-port`, в `release/release-12`) дал порты (`src/broker_contour/port.py`) и описание контура (`contours.py`). Адаптер выбирает и передаёт только `run.py`. Для Ф2 нужен `BrokerSession`: правило счёта, факты допуска, снимок, капитал, имена.
- Ф0 (`add-finam-probe`, ветка `feature/finam-probe`, не заархивирован) дал код и факты:
  - `src/finam/proto/` — заглушки Finam Trade API 2.24.0;
  - `src/finam/gateway.py` — `FinamGateway`: TLS-канал, `Auth`, JWT в `register_secrets`, `AuthService` без заголовка;
  - `src/finam/instruments.py` — `symbol` ↔ короткое имя через справочник Т-Инвест;
  - факты: «Факты разведки» в его design.md.

  До 30.11 Ф0 ещё дописывает факты и может править разведку.
- Ф2 — только чтение. Заявки, стопы, комиссии и поток заявок — Ф3.

Факты Ф0, на которых стоит Ф2:

| Тема | Факт | Что делает Ф2 |
|---|---|---|
| JWT | живёт 15 мин; `SubscribeJwtRenewal` присылает новый за минуту до конца; стримы при смене JWT не рвутся | стрим обновления; запасной путь — `Auth` по возрасту (уже в шлюзе Ф0) |
| Лимит | 200 в минуту на метод, окно — минута от первого вызова; подсказки повтора в ответе нет | ждать `reset_time` квоты из `GetUsageMetrics` |
| Окно | ежедневно 05:00–06:15 МСК API может быть недоступен | плановая недоступность, без тревоги |
| Счёт | единый счёт `UNION`; у пустого нет `equity` и `cash`; результат сделки — второй строкой в `cash`; закрытая позиция — строкой с `quantity` 0 | деньги — сумма строк RUB; нулевые позиции отбрасываются |
| Заявки | на другой день `GetOrders` отдаёт только активные; стоп — `ORDER_TYPE_STOP`, `WATCHING` | активные биржевые → `orders`, активные стопы → `stop_orders` |
| Символы | `SBER@MISX`, фьючерсы на RTSX; шаг совпал с Т-Инвест; лот фьючерса — `trade_lot_size` | соответствие по тикеру и площадке с проверкой шага и лота |
| ГО | своё, плавает (NG +17 % за ночь) | `GetAssetParams` при каждом чтении, без кэша |

## Goals / Non-Goals

**Goals:**

- Контур `finam` вида «счёт» работает рядом с `account` и `sandbox`: свой процесс, каталог, база, отпечаток, канал.
- Только чтение: допуск, снимок, сверка, `status.json`, API, UI.
- Общий код контура не знает Финам; меняется лишь то, что нужно любому брокеру с плановым окном обслуживания.

**Non-Goals:**

- Заявки, стопы, комиссии, `OrderFeed` Финама — Ф3. `trading()` у сессии Финама в Ф2 отказывает.
- Предохранители, сравнение `finam` с другими контурами, перенос привязок — Ф4.
- Демо-счёт Финама: поддержка не ответила.

## Decisions

### 1. Код Ф0 — merge-коммитом `feature/finam-probe`

```
release/release-12 ──●──────────────────────────────►
                      \                \
 feature/finam-probe   ●─●─●─ b4fa5c5 ─┼─●─● (факты до 30.11)
                                        \   \
 feature/finam-contour                   M───●───●──M' (повторное слияние без конфликтов)
                                         │
                       src/finam/{proto,gateway,instruments} ◄── src/broker_contour/finam/ (адаптер)
```

- Сейчас — `git merge --no-ff feature/finam-probe`. Заглушки, шлюз и справочник имён остаются одной копией в `src/finam/`. Адаптер их импортирует, а не копирует.
- Ф2 не правит файлы Ф0. Поэтому новые коммиты Ф0 (факты в его design.md, правки разведки) вливаются повторным `merge` без конфликтов.
- Нижние границы `grpcio>=1.81.1`, `protobuf>=6.33.5,<7` в `requirements.txt` приходят из Ф0 вместе с заглушками.
- Порядок PR: сначала Ф0 в `release/release-12` (после 30.11), затем Ф2. Если Ф2 окажется первым, его PR принесёт и коммиты Ф0 — тоже merge-коммитами, без переписывания истории.

**Отклонено:**

- *Перенести заглушки и шлюз в `src/broker_contour/finam/`.* Две копии ~8 тыс. строк сгенерированного кода. Факты и правки Ф0 после 30.11 пришлось бы переносить руками. После архива Ф0 одна копия стала бы мёртвой.
- *Ждать архива Ф0.* Ф2 стоял бы до 30.11 и дольше.

### 2. Адаптер `src/broker_contour/finam/`

```
 BrokerContour (общий код)          src/broker_contour/finam/                      src/finam/ (Ф0)
   admit_account ─► BrokerSession ─► session.py  FinamSession ──────────────┐
   _serve        ─►                  ├ api.py       FinamApi: лимит, окно ───┼──► gateway.py FinamGateway ─► api.finam.ru
   Reconciler                        ├ jwt.py       JwtRenewal: стрим JWT ───┤      (TLS, Auth, JWT-секреты)
                                     ├ account.py   правило счёта, снимок    │
                                     ├ admission.py факты допуска            │
                                     └ symbols.py   UID ↔ symbol, ГО ────────┴──► instruments.py (справочник Т-Инвест)
                                                                                    TINKOFF_TOKEN, только instruments.*
```

| Модуль | Что делает |
|---|---|
| `api.py` — `FinamApi` | все вызовы адаптера; повтор по лимиту; окно обслуживания |
| `jwt.py` — `JwtRenewal` | стрим `SubscribeJwtRenewal` в потоке-демоне; переподписка после обрыва |
| `account.py` | `account_rule` (`TokenDetails`), `read_account` (`GetAccount` + `GetOrders`), `capital` |
| `admission.py` | `read_facts`: права, квоты, предупреждения |
| `symbols.py` — `FinamSymbols` | `symbol` по карточке Т-Инвест и обратно, проверка шага и лота, `margin(symbol)` |
| `session.py` | `FinamSession` (`BrokerSession`) и `adapter(...)` для `run.py` |

### 3. JWT

- `FinamGateway.__enter__` делает `Auth` и регистрирует первый JWT. Затем `JwtRenewal` открывает `SubscribeJwtRenewal(secret)` (без заголовка, как требует `AuthService`).
- Каждый JWT из стрима проходит `gateway.adopt_jwt(token)`, и только потом используется: `register_secrets` → заглушка `jwt#N` → текущий JWT шлюза.
- Стрим оборвался → переподписка через 5, 10, 30, 60 с. Пока стрима нет, работает запасной путь шлюза Ф0: JWT старше 10 минут → новый `Auth` перед вызовом.
- В Ф2 стримов заявок нет. В Ф3 они живут на старом JWT: по факту ж Финам проверяет JWT только при открытии стрима.

### 4. Снимок счёта

```
GetAccount ─┬─ cash[]:  RUB 30000, RUB −8,21    ──► rub = Σ RUB = 29 991,79   (пустой счёт: cash нет → 0)
            ├─ equity                           ──► liquid_portfolio            (пустой счёт: нет → None)
            └─ positions[]: symbol, quantity    ──► quantity ≠ 0 → (короткое имя, лоты)
GetOrders ──┬─ активные биржевые                ──► orders      (короткое имя на заявку)
            └─ активные ORDER_TYPE_STOP/STOP_LIMIT ► stop_orders (короткое имя на стоп)
```

- Активная заявка — статус не из конечных Ф0 (`FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`, `EXECUTED` …). Список берётся из `src/finam/orders.py`.
- Лоты позиции:
  - фьючерс — `quantity` контрактов (`trade_lot_size = 1`);
  - акция — `quantity` штук ÷ лот карточки Т-Инвест; нецелое — `BrokerCallError(INCONSISTENT)`, как противоречивый снимок Т-Инвест.
- Валюта, отличная от RUB, в `cash` в Ф2 не складывается с рублями: строки не-RUB пишутся в лог числом, без сумм.
- `names`: UID Т-Инвест → короткое имя, когда соответствие найдено; иначе `symbol` → имя справочника или «инструмент не указан».

### 5. Соответствие инструментов — `symbols.py`

«Карточка ядра» — карточка Т-Инвест: по ней ядро строит свечи и сигналы. Финам о ней не знает, поэтому соответствие строится по биржевому тикеру.

| Направление | Как |
|---|---|
| карточка Т-Инвест → `symbol` | `Assets`: тикер совпал, не архивный, площадка RTSX у фьючерса и MISX у акции |
| `symbol` → короткое имя и UID | тикер `symbol` → фьючерсы справочника Т-Инвест (`active_futures_with_expiry`) или акция по тикеру |
| проверка | шаг `min_step / 10^decimals` = шаг Т-Инвест; лот: фьючерс — `trade_lot_size`, акция — `lot_size` = лот Т-Инвест |

- Расхождение шага или лота — `SymbolMismatch` с коротким именем и полем. В Ф2 позиция показывается, в лог идёт предупреждение. В Ф3 привязка с таким инструментом на Финаме не допускается.
- Справочник Т-Инвест — `ReferenceGateway` Ф0 на `TINKOFF_TOKEN`: тот же токен ядра, только `instruments.*`. `Assets` читается раз за процесс, карточки — по требованию с кэшем.
- **ГО:** `margin(symbol)` → `GetAssetParams` (`long_initial_margin`, `short_initial_margin`) при каждом вызове, без кэша. В Ф2 ГО попадает только в факты допуска по позициям счёта; потребитель — Ф3.

### 6. Лимит и окно обслуживания — `api.py`

```
call(method) ─► gateway.call(retry=False)
   ├─ ок ─────────────────────────────────────────────► ответ
   ├─ RESOURCE_EXHAUSTED ─► GetUsageMetrics ─► reset_time квоты метода
   │      ─► ждать до reset_time + 1 с (не больше 65 с) ─► повтор (до 2 раз) ─► BrokerCallError RESOURCE_EXHAUSTED
   └─ UNAVAILABLE / DEADLINE_EXCEEDED / UNAUTHENTICATED при Auth
          ├─ 05:00–06:15 МСК ─► BrokerCallError(code = PLANNED_OUTAGE)
          └─ иначе ────────────► BrokerCallError(как есть)
```

- Квота ищется по имени метода (`AccountsService.GetAccount`). Нет квоты или сбой `GetUsageMetrics` → пауза 60 с.
- Повтор шлюза Ф0 (1, 2, 4 с) адаптер выключает: `retry=False` на каждом вызове.
- `PLANNED_OUTAGE = "PLANNED_OUTAGE"` — новая константа `port.py`. Это общий признак «брокер в плановом окне», не Финам. Общий код поступает так:

| Где | Сейчас | С `PLANNED_OUTAGE` |
|---|---|---|
| допуск при старте | отказ, тревога, выход 2 | `starting`, причина «окно обслуживания брокера», повтор допуска раз в минуту до успеха или остановки |
| сверка | пропуск; третий подряд — тревога «Счёт недоступен» | пропуск, счётчик не растёт, тревоги нет |
| `status.json` | `account_link: lost` | `account_link: maintenance` |
| снимок при остановке | `account_link: lost` | `account_link: maintenance` |

Т-Инвест `PLANNED_OUTAGE` не возвращает: у «Счёта» и песочницы поведение прежнее.

### 7. Допуск и отпечаток

```
check_settings ─► FINAM_SECRET_TOKEN, FINAM_ACCOUNT_ID, бот, канал          (без сети, без тревоги)
find_account   ─► TokenDetails: account_ids ровно один и = FINAM_ACCOUNT_ID  (иначе тревога)
                  readonly → access_level read_only | full_access
read_facts     ─► GetUsageMetrics → limits; warnings
check_fingerprint(broker = "finam")  в data/contours/finam/broker.sqlite3
read_account   ─► снимок; непустой счёт на первом старте — первая тревога сверки (как у «Счёта»)
```

- Все номера счетов из `TokenDetails` — в `register_secrets` до проверки. В причине — только последние 4 символа.
- `facts` — та же схема, что у «Счёта», чтобы API и UI не ветвились:

| Поле | Финам |
|---|---|
| `access_level` | `read_only` или `full_access` по `readonly` |
| `qualified_for_work_with` | `[]` — у Финама нет |
| `derivatives` | `null` — Финам не сообщает |
| `tariff` | `null` |
| `limits` | `orders` и `stop_orders` — квота `PlaceOrder`, `operations` — квота `Transactions` |
| `warnings` | «квоты API не прочитаны», если `GetUsageMetrics` не ответил |

- При `orders = true` `read_facts` требует `full_access`. В Ф2 до этого не доходит: конфиг отклоняет `orders = true` раньше.

### 8. Сборка

| Что | Значение |
|---|---|
| описание | `LiveContour("finam", kind="account", broker="finam", adapter="finam-account", title="СЧЁТ ФИНАМ", token_env="FINAM_SECRET_TOKEN", account_id_env="FINAM_ACCOUNT_ID")` |
| порядок подъёма | `account`, `finam`, `sandbox` |
| надзиратель | свой `BrokerContourSupervisor` из цикла по описаниям; без целевого состояния не стартует сам |
| каталог | `data/contours/finam/` |
| `run.py` | `_broker_sessions`: `finam-account` → `finam.adapter(token, account_id, reference_token=TINKOFF_TOKEN)`; `_SECRETS` += `FINAM_SECRET_TOKEN`, `FINAM_ACCOUNT_ID`, токен бота Финама |
| конфиг | `[contours.finam]` — ключи `[contours.account]`; `orders = true` → `ConfigError` «[contours.finam] orders = true: в этой версии контур Финама только читает счёт» |
| Telegram | свой бот и канал: `telegram_bot_env = "TELEGRAM_FINAM_BOT_TOKEN"`, `telegram_channel_env = "TELEGRAM_FINAM_CHANNEL_ID"` (имена — open question) |
| заголовки | `CONTOUR_TITLES` из описания; `console_view` берёт метку из `CONTOUR_TITLES`, а не из своего словаря |
| `status.json` | новое поле `broker`: `tinvest` или `finam` (у песочницы `tinvest`) |
| PyInstaller | `run.spec`: `src.finam.proto.*` и `src.broker_contour.finam` в `hiddenimports` |

Граница общего кода (тест Ф1) дополняется `src.finam`.

### 9. API `/api/v1/finam/*`

`AccountControl` получает параметры экземпляра вместо констант класса. Маршруты строятся от базового пути.

```
                       AccountControl(contour_id, base, title, orders_enabled, section)
 /api/v1/account/* ──► account  "/api/v1/account"  «Счёт»        [contours.account]
 /api/v1/finam/*   ──► finam    "/api/v1/finam"    «Счёт Финам»  [contours.finam]   orders_enabled = False
```

| Что | Как |
|---|---|
| чтения | сводка, `target`, `bindings`, `reconcile`, `log`, `positions`, `performance`, `trades`, `account` — те же схемы, что у `/api/v1/account` |
| команды состава | `409 ACCOUNT_READ_ONLY`, текст называет `[contours.finam] orders = false` |
| запуск и остановка | как у «Счёта» при `orders = false`: `PUT …/target` `on` / `no_entries` / `close_all` + `confirm` |
| `reconcile` | без `fee_probes` у Финама: поле `null` (разведка комиссий — только Т-Инвест) |
| `GET /api/v1` | ключ и ссылка `finam`; сводка `finam` рядом с `account` |
| сводка и `GET …` контура | поле `broker`: `tinvest` у `account`, `finam` у `finam` |
| `account_link` | в перечне значений добавлено `maintenance` |
| сравнение | `/api/v1/finam/comparison` нет (Ф4). `AccountComparison` принимает имя своей стороны вместо `"account"`; `SIDES` строятся от него |
| `export_api` | таблица «базовый путь → ресурс контура на брокере» вместо пар `if path == views.ACCOUNT`; пути `sandbox` и `account` прежние |
| OpenAPI | 2.13.0 → 2.14.0, тег «Счёт Финам», только добавления |

### 10. UI (репозиторий `UI`)

Ветка `feature/ui-finam-contour` от текущей рабочей `feature/ui-clusters-chart`, без push.

- `TradingContour`, `BrokerContour` += `'finam'`.
- `queries.ts`: таблица `CONTOUR_BASE = {emulation, sandbox, account, finam}` вместо цепочек тернарников.
- `AccountPage` с параметром `contour` (`account` | `finam`), маршрут `/finam`. Пункт меню «Счёт Финам» — только при ключе `finam` в `GET /api/v1`.
- «Обзор»: карточка «Счёт Финам». «Аварийная остановка»: строка `finam`. Шапка страницы: метка брокера по `broker`.
- Только Ant Design и уже подключённое. После правок `npm run build`. Проверка — на тестовом роботе 8102.

### 11. Тесты

- **Фейковый API Финама.** Берётся `tests/unit/finam/fakes.py` Ф0 и расширяется: `SubscribeJwtRenewal`, квоты с `reset_time`, недоступность в заданное время.
- **Адаптер:**
  - правило счёта;
  - снимок: пустой счёт, две строки `cash`, нулевая позиция, акция в штуках;
  - активные и конечные заявки, стопы;
  - соответствие и расхождение шага и лота;
  - ГО без кэша;
  - повтор по `reset_time`;
  - окно обслуживания;
  - смена JWT стримом и обрыв стрима.
- **Маскирование.** `FINAM_SECRET_TOKEN`, `FINAM_ACCOUNT_ID` и каждого JWT нет в логе, `status.json`, событиях канала и ответах API.
- **Два живых контура.** `account` и `finam` одновременно: свои каталоги, базы и отпечатки; отпечаток `finam` не принимает счёт другого брокера.
- **Общий код.** `PLANNED_OUTAGE` при допуске и сверке; «Счёт» без него — прежние тревоги.
- **Конфиг `[contours.finam]`** и **API `/api/v1/finam`**: чтения, `409`, ключ `finam`, `broker`.
- Существующие тесты проверок не меняют.

## Risks / Trade-offs

- **Ф0 ещё живой.** Если Ф0 после слияния поменяет шлюз несовместимо, адаптер сломается на повторном `merge`. Смягчение: адаптер опирается на четыре точки шлюза — `call`, `open_stream`, `adopt_jwt`, `jwt` — и тесты адаптера идут на настоящем `FinamGateway` с фейковыми заглушками.
- **Справочник Т-Инвест в процессе Финама.** Контур `finam` вызывает `instruments.*` на `TINKOFF_TOKEN`. Нагрузка — один список фьючерсов за процесс и карточки по требованию; квоты Т-Инвест на `instruments` велики.
- **Окно обслуживания по часам Mac.** Ошибка вне окна — обычная недоступность с тревогой. Окно задано константой; если Финам его сдвинет, правка в одном месте.
- **Еще один процесс и канал** — ещё один бот. Если владелец выберет общий бот «Счёта», меняются только имена переменных в `[contours.finam]`.

## Migration Plan

- Данные не переносятся. `data/contours/finam/` создаётся при первом старте.
- Без `FINAM_SECRET_TOKEN` и без команды запуска контур не поднимается и ничего не пишет в канал.
- Откат — предыдущий релиз; каталог `finam` ему не мешает.

## Open Questions

1. **Код Ф0.** Merge-коммит `feature/finam-probe` (рекомендуется) или перенос в `src/broker_contour/finam/`?
2. **Telegram.** Свой бот `TELEGRAM_FINAM_BOT_TOKEN` и канал `TELEGRAM_FINAM_CHANNEL_ID`? Или бот «Счёта» `TELEGRAM_ACCOUNT_BOT_TOKEN` и свой канал?
3. **Справочник имён Финама на `TINKOFF_TOKEN`** (только `instruments.*`) — согласны?
