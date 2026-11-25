## Context

- Ф1 (`refactor-broker-port`, в `release/release-12`) дал порты (`src/broker_contour/port.py`) и описание контура (`contours.py`). Адаптер выбирает и передаёт только `run.py`.
- Ф0 (`add-finam-probe`, ветка `feature/finam-probe`, не заархивирован) дал:
  - `src/finam/proto/` — заглушки Finam Trade API 2.24.0;
  - разведку (`src/finam/gateway.py`, `instruments.py`, команды);
  - факты — раздел «Факты разведки» в его design.md.

  До 30.11 Ф0 ещё дописывает факты и может править разведку.
- Ф2 — только чтение. Заявки, стопы, комиссии и поток заявок — Ф3.

Факты Ф0, на которых стоит Ф2:

| Тема | Факт | Что делает Ф2 |
|---|---|---|
| JWT | живёт 15 мин; `SubscribeJwtRenewal` присылает новый за минуту до конца; стримы при смене JWT не рвутся | один `JwtSource`: стрим основной, `Auth` запасной |
| Лимит | 200 в минуту на метод; подсказки повтора в ответе нет | ждать `reset_time` квоты из `GetUsageMetrics` |
| Окно | ежедневно 05:00–06:15 МСК API может быть недоступен | `BrokerOutage`, без тревоги |
| Счёт | у пустого счёта нет `equity` и `cash`; результат сделки — второй строкой в `cash`; закрытая позиция — строкой с `quantity` 0 | деньги — сумма строк RUB; нулевые позиции отбрасываются |
| Заявки | на другой день `GetOrders` отдаёт только активные; стоп — `ORDER_TYPE_STOP` | активные биржевые → `orders`, активные стопы → `stop_orders` |
| Символы | `SBER@MISX`, фьючерсы на RTSX; шаг совпал с Т-Инвест; лот фьючерса — `trade_lot_size` | соответствие по тикеру и площадке с проверкой шага и лота |
| ГО | своё, плавает (NG +17 % за ночь) | `GetAssetParams` при каждом чтении, без кэша |

## Goals / Non-Goals

**Goals:**

- Контур `finam` вида «счёт» работает рядом с `account` и `sandbox`: свой процесс, каталог, база, отпечаток, канал. Только чтение: допуск, снимок, сверка, `status.json`, API, UI.
- Общий код не ветвится по брокеру и номеру контура — ни в процессе, ни в конфиге, ни в API. Различия — данные описания или то, что передаёт `run.py`.
- Рабочий код не зависит от кода разведки Ф0, а адаптер Финама — от адаптера Т-Инвест.
- Ф3 добавляет торговлю Финама, не трогая общий код.

**Non-Goals:**

- Заявки, стопы, комиссии, `OrderFeed` Финама — Ф3.
- Предохранители, сравнение `finam` с другими контурами, перенос привязок — Ф4.
- Разбор `BrokerContour` и `SandboxControl` на части сверх нужного здесь.
- Демо-счёт Финама.

## Decisions

### 1. Схема зависимостей

```
 run.py — точка сборки
   │
   ├─ tinvest.catalog(TINKOFF_TOKEN) ──► InstrumentCatalog (порт: карточки ядра)
   │                                          │ передаётся
   ├─ finam.adapter(secret, account_id, catalog) ──► BrokerSession (чтение)
   │      src/broker_contour/finam/
   │        transport.py  канал, JwtSource, лимит, окно ──► src/finam/proto (заглушки)
   │        account.py · admission.py · symbols.py
   │
   └─ BrokerContour(session_factory)            общий код: port.py, process.py, reconcile.py …
                                                 знает только порты

 Разведка Ф0 (src/finam/gateway.py, instruments.py, команды) — сама по себе; адаптер её не импортирует.
```

Направления зависимостей:

- общий код → порты;
- адаптер → порты и заглушки proto;
- `run.py` → все.

Адаптер Финама не импортирует `src.broker_contour.tinvest`, `src.sandbox`, `t_tech` и разведку `src.finam` (кроме `src.finam.proto`). Это проверяет тест границ, как в Ф1.

### 2. Код Ф0 — merge-коммит ради заглушек

- `git merge --no-ff feature/finam-probe`. Из Ф0 адаптеру нужны только `src/finam/proto/` и нижние границы `grpcio`/`protobuf` в `requirements.txt`. Разведка остаётся владельцу как инструмент.
- Ф2 не правит файлы Ф0. Факты и правки Ф0 до 30.11 вливаются повторным `merge` без конфликтов: адаптер не зависит от шлюза разведки, поэтому её правки его не ломают.
- Порядок PR: Ф0 в `release/release-12` раньше Ф2. Если Ф2 окажется первым, его PR несёт коммиты Ф0 merge-коммитами.

**Отклонено:**

- *Адаптер на `FinamGateway` и `Instruments` разведки.* Рабочий код зависел бы от инструмента разведки, который ещё меняется. Унаследованные повтор и обновление JWT пришлось бы выключать (`retry=False`), а `Instruments` тянет закрытые `_family` и `_Service` из `src.sandbox`.
- *Заново сгенерировать заглушки в `src/broker_contour/finam/proto/`.* Две копии ~8 тыс. строк сгенерированного кода.
- *Ждать архива Ф0.* Ф2 стоял бы до 30.11 и дольше.

Своего кода у транспорта около 150 строк. Из разведки в него переходят только идеи, проверенные прогонами: `AuthService` без заголовка, разбор текста ошибки Финама. Позже разведку можно перевести на транспорт адаптера отдельной задачей, а можно оставить как есть.

### 3. Порт сессии делится на чтение и торговлю (ISP, LSP)

```
 BrokerSession (чтение)            TradingSession(BrokerSession)
   find_account                       capital
   read_facts                         short_name
   read_account                       trading → TradingLink
   fee_probes → None | разведка
       ▲                                  ▲
   FinamSession (Ф2)                 TInvestSession (как есть)     FinamSession (Ф3)
```

- `BrokerContour._serve` требует `BrokerSession`, `_serve_trading` — `TradingSession`.
- В описании контура появляется `trading: bool`, то есть умеет ли адаптер заявки. `run.py` собирает контур с `orders = true`, только если `trading`. Загрузка конфига отклоняет `[contours.<номер>] orders = true` у описания с `trading = False`. Правило одно для всех контуров, без упоминания Финама.
- `TInvestSession` не меняется: она уже реализует оба протокола.
- `fee_probes` остаётся необязательной возможностью чтения, как в Ф1. Финам отдаёт `None`.

### 4. Порт `InstrumentCatalog` — карточки ядра

«Карточка ядра» — карточка Т-Инвест: по ней ядро строит свечи и сигналы. Адаптер Финама получает каталог из `run.py`, а не ходит к Т-Инвест сам (DIP).

```python
@dataclass(frozen=True)
class InstrumentCard:          # port.py
    uid: str                   # UID Т-Инвест — ключ инструмента ядра и журнала
    ticker: str                # биржевой тикер
    kind: str                  # share · future
    short_name: str            # SBER · Si-12.26
    lot: int
    price_step: Decimal

class InstrumentCatalog(Protocol):
    def by_uid(self, uid: str) -> InstrumentCard | None: ...
    def by_ticker(self, ticker: str, kind: str) -> InstrumentCard | None: ...
```

- Реализация — `src/broker_contour/tinvest/catalog.py`. Шлюз Т-Инвест на `TINKOFF_TOKEN` пропускает только `instruments.*`, остальное отклоняет до сети. Короткое имя каталог берёт из существующей `short_name_by_uid`, а не пишет третью копию правила. Карточки кэшируются на процесс.
- В Ф3 тот же каталог понадобится торговле Финама для заявок по UID журнала.

### 5. Адаптер `src/broker_contour/finam/`

| Модуль | Что делает |
|---|---|
| `transport.py` — `FinamTransport` | TLS-канал `api.finam.ru:443`; `call(method, request)`; ошибки → `BrokerCallError` с текстом Финама в одну строку; повтор по лимиту; окно обслуживания |
| `transport.py` — `JwtSource` | единственный владелец JWT (см. 6) |
| `symbols.py` — `FinamSymbols` | `symbol` по карточке каталога и обратно, проверка шага и лота, `margin(symbol)` |
| `account.py` | правило счёта (`TokenDetails`), снимок (`GetAccount` + `GetOrders`) |
| `admission.py` | факты допуска (см. 9) |
| `session.py` | `FinamSession` (`BrokerSession`), `adapter(secret, account_id, catalog_factory)` |

### 6. JWT — один источник

```
 JwtSource ── lock ── current JWT ◄─┬─ стрим SubscribeJwtRenewal (поток-демон): новый JWT за минуту до конца
                                    └─ Auth(secret): при входе и если стрима нет, а JWT старше 10 мин
 каждый JWT: register_secrets → потом current
 FinamTransport.call() ──► JwtSource.token() ──► metadata authorization
```

- Один объект владеет JWT. Чтение и запись — под замком; вызов никогда не видит JWT, который ещё не стал секретом.
- Стрим оборвался → переподписка через 5, 10, 30, 60 с. Пока стрима нет, `token()` при необходимости делает `Auth` сам.
- Стримы Ф3 открываются с текущим JWT и при смене не переоткрываются (факт ж).

### 7. Лимит и окно обслуживания — в транспорте, один раз

```
call(method)
   ├─ ок ─────────────────────────────────────────────────────────► ответ
   ├─ RESOURCE_EXHAUSTED ─► GetUsageMetrics ─► reset_time квоты метода
   │      ─► ждать до reset_time + 1 с (не дольше 65 с; нет квоты — 60 с) ─► повтор (до 2) ─► BrokerCallError
   └─ UNAVAILABLE · DEADLINE_EXCEEDED · отказ Auth
          ├─ 05:00–06:15 МСК ─► BrokerOutage(BrokerCallError)   плановое окно
          └─ иначе ───────────► BrokerCallError                 как есть
```

`BrokerOutage` — подкласс `BrokerCallError` в `port.py`. Общий код, который не знает о нём, видит обычную ошибку вызова. Окно задано константой адаптера.

### 8. Плановое окно в общем коде — в одном месте

Сейчас процесс ловит `BrokerCallError` при чтении счёта в трёх местах: `_reconcile`, `_reconcile_trading`, `_final_account`. Ф2 сводит их в один метод, а не добавляет четвёртую проверку:

```python
def _read(self, session, account) -> AccountRead:        # snapshot | lost | maintenance
```

| Исход | `status.json` | сверщик |
|---|---|---|
| `snapshot` | `account_link: ok` | сверка |
| `lost` (`BrokerCallError`) | `account_link: lost` | пропуск, счётчик +1, на третьем — тревога (как сейчас) |
| `maintenance` (`BrokerOutage`) | `account_link: maintenance` | пропуск, счётчик не растёт, тревоги нет |

`Reconciler.run(now, None)` получает признак плановости (`planned=True`).

**Допуск.** `admit_account` бросает `BrokerOutage` наружу, а не превращает его в отказ. `BrokerContour.run` ждёт его в одном цикле: `starting`, причина «окно обслуживания брокера», повтор раз в минуту, выход по остановке.

У Т-Инвест `BrokerOutage` не бывает, поэтому «Счёт» и песочница работают как прежде. Это проверяют их существующие тесты.

### 9. Допуск и факты — без выдуманных полей

```
check_settings ─► секрет, номер счёта, бот, канал (без сети, без тревоги)
find_account   ─► TokenDetails: номера → register_secrets; ровно один и = FINAM_ACCOUNT_ID
read_facts     ─► readonly → access_level; GetUsageMetrics → limits
check_fingerprint(broker = "finam") ─► read_account
```

Факты — общая часть плюс сведения своего брокера:

| Поле | «Счёт» Т-Инвест | Финам |
|---|---|---|
| `access_level` | как сейчас | `read_only` / `full_access` по `readonly` |
| `limits` | как сейчас | квоты методов, которые вызывает контур: `{"GetAccount": 200, …}` |
| `warnings` | как сейчас | «квоты API не прочитаны», если `GetUsageMetrics` не ответил |
| `qualified_for_work_with`, `derivatives`, `tariff` | как сейчас | нет |

- В OpenAPI `AccountAdmission` не меняется. У Финама своя схема `FinamAdmission` — общие поля без полей Т-Инвест.
- Сверка Финама — общая схема `ReconcileReport` песочницы, без `fee_probes`. `AccountReconcileReport` остаётся у «Счёта».

### 10. Снимок счёта

```
GetAccount ─┬─ cash[]: RUB 30000, RUB −8,21      ──► rub = Σ RUB              (нет cash → 0)
            ├─ equity                            ──► liquid_portfolio          (нет → None)
            └─ positions[]: symbol, quantity ≠ 0 ──► (short_name, лоты), names[uid] = short_name
GetOrders ──┬─ активные биржевые                 ──► orders
            └─ активные STOP / STOP_LIMIT        ──► stop_orders
```

- Лоты: фьючерс — `quantity`; акция — `quantity` ÷ лот карточки. Нецелое — `BrokerCallError(INCONSISTENT)`, как противоречивый снимок Т-Инвест.
- `names` — всегда UID Т-Инвест → короткое имя, как у Т-Инвест. Символ без карточки в каталоге попадает в снимок как «инструмент не указан» с тикером в логе и в `names` не входит: смешанных ключей нет.
- Строки `cash` не в RUB с рублями не складываются; в лог пишется только их число.

### 11. Инструменты — `symbols.py`

| Направление | Как |
|---|---|
| карточка → `symbol` | `Assets` (раз за процесс): тикер совпал, не архивный; площадка RTSX у фьючерса, MISX у акции |
| `symbol` → карточка | тикер из `symbol` → `catalog.by_ticker(ticker, kind по площадке)` |
| проверка | шаг `min_step / 10^decimals` = `price_step`; лот: фьючерс — `trade_lot_size`, акция — `lot_size` = `lot` |
| ГО | `margin(symbol)` → `GetAssetParams` при каждом вызове |

- Расхождение шага или лота — `SymbolMismatch(short_name, поле)`. В Ф2: позиция в снимке, предупреждение в лог. В Ф3: привязка на Финаме не допускается.
- В Ф2 ГО никто не вызывает, кроме тестов. Метод нужен Ф3 и делается здесь, потому что опирается на то же соответствие.

### 12. Описание контура и конфиг — данные вместо веток

```python
@dataclass(frozen=True)
class LiveContour:
    contour_id: str; kind: str
    broker: str            # отпечаток в broker.sqlite3 (как в Ф1): account · finam · sandbox
    provider: str          # брокер адаптера: tinvest · finam   → status.json и API "broker"
    adapter: str; title: str; token_env: str; account_id_env: str | None
    trading: bool          # адаптер умеет заявки
    defaults: dict         # значения [contours.<номер>] по умолчанию: имена переменных бота и канала
```

| Номер | Брокер | Провайдер | Адаптер | Заголовок | Токен | Счёт | trading |
|---|---|---|---|---|---|---|---|
| `account` | `account` | `tinvest` | `tinvest-account` | СЧЁТ | `TINKOFF_TRADE_TOKEN` | `TINKOFF_ACCOUNT_ID` | да |
| `finam` | `finam` | `finam` | `finam-account` | СЧЁТ ФИНАМ | `FINAM_SECRET_TOKEN` | `FINAM_ACCOUNT_ID` | нет (Ф3) |
| `sandbox` | `sandbox` | `tinvest` | `tinvest-sandbox` | ПЕСОЧНИЦА | `TINKOFF_SANDBOX_TOKEN` | — | да |

- Порядок подъёма: `account`, `finam`, `sandbox`.
- **Конфиг.** `config_loader` строит набор ключей и проверки секции контура на брокере по описанию: ключи вида `account`/`sandbox` (сейчас — два одинаковых множества), имена переменных из `defaults`, `orders = true` при `trading = False` → `ConfigError` «[contours.finam] orders = true: адаптер этого контура пока не умеет заявок». Третьего множества ключей и списка имён в коде нет. Плоские имена `contours_<номер>_<ключ>` и значения у `account` и `sandbox` прежние.
- **`run.py`:**
  - `_broker_sessions` выбирает адаптер по `spec.adapter`;
  - `_SECRETS` дополняется переменными из описаний (`token_env`, `account_id_env`, бот), а не списком вручную;
  - надзиратели строятся циклом, как в Ф1.
- **Заголовки.** `console_view` берёт метку из `CONTOUR_TITLES` — его свой словарь с «ПЕСОЧНИЦА»/«СЧЁТ» удаляется.
- **`status.json`:** новое поле `broker` = `provider`.

### 13. API — ресурс контура получает свои части извне (OCP)

```
 run.py ─► LiveAccountResource(contour_id, base, title, provider,
                               orders_enabled,         # из [contours.<номер>] orders
                               reconcile_extras,       # account: fee_probes · finam: None
                               comparison)             # account (orders): AccountComparison · finam: None
 export_api ─► LIVE = {"/api/v1/account": ресурс, "/api/v1/finam": ресурс}
               маршрут → ресурс.routes() → обработчик        (без if по номеру контура)
```

- `AccountControl` получает параметры экземпляра вместо констант класса. Наследование от `SandboxControl` остаётся (Non-Goal), но без новых веток.
- Ресурс сам объявляет `routes()`. `comparison` и `guards/reset` есть, если переданы сравнение и торговля, иначе `404`.
- `export_api`:
  - `_account_get` / `_account_command` становятся общими обработчиками ресурса по таблице `LIVE`;
  - `GET /api/v1` перечисляет `LIVE` (ключи `account`, `finam`) со сводками;
  - ресурсы торговли (`positions`, `trades`, `performance`, `account`) — по той же таблице.
- `AccountComparison` принимает имя своей стороны, `SIDES` строятся от него; путь и ответ `account` прежние.
- Сводка и `GET` контура — поле `broker` (`provider`); `account_link` — плюс `maintenance`.
- Команды состава при `orders_enabled = False` — `409 ACCOUNT_READ_ONLY`, текст называет секцию контура.
- OpenAPI 2.13.0 → 2.14.0, только добавления:
  - тег «Счёт Финам», пути `/api/v1/finam/*`;
  - схемы `Finam`, `FinamCurrent`, `FinamAdmission`;
  - `broker` у `Account`;
  - `maintenance` в `account_link`.

### 14. UI (репозиторий `UI`)

Ветка `feature/ui-finam-contour` от текущей рабочей `feature/ui-clusters-chart`, без push.

- `TradingContour`, `BrokerContour` += `'finam'`; в `queries.ts` таблица `CONTOUR_BASE` вместо цепочек тернарников.
- `AccountPage` с параметром `contour`, маршрут `/finam`, пункт меню «Счёт Финам» при ключе `finam` в `GET /api/v1`.
- Блок допуска показывает общие поля и поля своего брокера, если они есть: без проверки «это Финам».
- Карточка в «Обзоре», строка в «Аварийной остановке», метка брокера по `broker`.
- Только Ant Design и уже подключённое; `npm run build`; проверка на 8102.

### 15. Тесты

- **Фейк Финама** — свой, в `tests/unit/broker_contour/finam/fakes.py`, на заглушках proto. Фейк разведки Ф0 не используется: тесты Ф2 не зависят от тестов Ф0.
- **Адаптер:**
  - транспорт: лимит по `reset_time`, окно, текст ошибки;
  - `JwtSource`: стрим, обрыв, запасной `Auth`, JWT — секрет до использования;
  - правило счёта, снимок (пустой счёт, две строки `cash`, нулевая позиция, акция в штуках, заявки и стопы), соответствие и расхождение, ГО без кэша.
- **Каталог Т-Инвест** — карточки по UID и тикеру, отказ не-`instruments.*` до сети.
- **Границы** — общий код без `src.finam`; адаптер Финама без `tinvest`, `src.sandbox`, `t_tech` и разведки.
- **Общий код:**
  - `BrokerOutage` при допуске, сверке и остановке;
  - «Счёт» и песочница без него — прежние тревоги;
  - `orders = true` у описания без `trading` — `ConfigError`.
- **Регресс владельца** — маскирование, два живых контура, конфиг, API (см. tasks).
- Существующие тесты проверок не меняют. Перестройка `_read` и `config_loader` проверяется именно ими.

## Risks / Trade-offs

- **Больше переделок общего кода, чем «просто добавить контур»:** `_read` в процессе, ключи конфига по описанию, `LIVE` в `export_api`, разделение сессии. Зато Ф3 и следующий брокер не трогают общий код. Каждый шаг — коммит с зелёным полным `pytest`.
- **Транспорт пишется заново, а не берётся из разведки.** Около 150 строк повторяют проверенные идеи Ф0 (без заголовка у `AuthService`, разбор ошибок). Это осознанный дубль ради правильного направления зависимостей; разведку можно перевести на транспорт позже.
- **Схема фактов и сверки Финама отличается от «Счёта».** Это отход от «тех же схем»: поля «Счёта» не меняются, а Финам не отдаёт выдуманных значений. UI показывает общие поля.
- **Справочник Т-Инвест в процессе Финама** — один список и карточки по требованию на `TINKOFF_TOKEN`, только `instruments.*`.
- **Окно по часам Mac.** Вне окна — обычная недоступность с тревогой. Окно — одна константа.

## Migration Plan

- Данные не переносятся. `data/contours/finam/` создаётся при первом старте.
- Без `FINAM_SECRET_TOKEN` и команды запуска контур не поднимается.
- Ключи и значения `[contours.account]`, `[contours.sandbox]` прежние.
- Откат — предыдущий релиз.

## Open Questions

1. **Telegram.** Свой бот `TELEGRAM_FINAM_BOT_TOKEN` и канал `TELEGRAM_FINAM_CHANNEL_ID`? Или бот «Счёта» `TELEGRAM_ACCOUNT_BOT_TOKEN` и свой канал?
2. **Каталог карточек на `TINKOFF_TOKEN`** (только `instruments.*`) в процессе Финама — согласны?
