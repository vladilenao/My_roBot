## Why

Финам подключается вторым реальным брокером из-за комиссий Т-Инвест. Т-Инвест не заменяется: оба брокера работают одновременно, свечи и анализ остаются у ядра на Т-Инвест, Финам только исполняет. Этапы Ф0–Ф5; это **Ф2 — контур `finam` только на чтение**.

Ф1 (`refactor-broker-port`) вынес брокера за порты, Ф0 (`add-finam-probe`) разведал Finam Trade API. Теперь нужен второй живой контур вида «счёт» на Финаме, который видит счёт, сверяется и показывает его в API и UI, но не ставит заявок. Так проверяется весь путь Финама до денег: допуск, JWT, лимиты, окно обслуживания, снимок, сверка.

## What Changes

```
 ядро (TINKOFF_TOKEN: свечи, анализ, карточки)
   ├─ account  → Т-Инвест   data/contours/account/  СЧЁТ          (без изменений)
   ├─ finam    → Финам      data/contours/finam/    СЧЁТ ФИНАМ    (новый, orders = false)
   └─ sandbox  → Т-Инвест   data/contours/sandbox/  ПЕСОЧНИЦА     (без изменений)
```

- **Код Ф0 в ветку** — merge-коммитом `feature/finam-probe` (design, решение 1). Заглушки proto и шлюз остаются в `src/finam/`, адаптер их использует.
- **Адаптер `src/broker_contour/finam/`** на чтение — `BrokerSession` порта Ф1:
  - JWT обновляется стримом `SubscribeJwtRenewal`, каждый JWT сразу становится секретом лога;
  - снимок счёта из `GetAccount` и `GetOrders`;
  - соответствие инструмента Т-Инвест и `symbol` Финама;
  - ГО из `GetAssetParams` при каждом чтении;
  - повтор по лимиту ждёт `reset_time` из `GetUsageMetrics`;
  - окно обслуживания 05:00–06:15 МСК — плановая недоступность без тревоги.
- **Допуск:** токен видит ровно один счёт `FINAM_ACCOUNT_ID`; права по `TokenDetails.readonly`; отпечаток `broker = finam`; отказ — тревогой в канал.
- **Сборка:** описание контура `finam`, свой надзиратель ядра, `[contours.finam]` с ключами `[contours.account]`, свой бот и канал, заголовок «СЧЁТ ФИНАМ». `orders = true` в Ф2 отклоняется при загрузке конфига.
- **API `/api/v1/finam/*`** — зеркало `/api/v1/account/*` с теми же схемами. Команды состава — `409 ACCOUNT_READ_ONLY`. В `GET /api/v1` — ключ `finam`; у `account` и `finam` — поле `broker`. OpenAPI 2.13.0 → 2.14.0, только добавления.
- **Обобщение по `contour_id`:** `account_control`, `comparison_view`, `export_api`. Пути `sandbox` и `account` не меняются.
- **UI** (отдельный репозиторий): страница «Счёт Финам», карточка в «Обзоре», строка в «Аварийной остановке», метка брокера.

**Не входит:** заявки, стопы, комиссии, поток заявок — Ф3; предохранители, сравнение, перенос привязок — Ф4.

## Capabilities

### New Capabilities

- `finam-contour`: контур `finam` на Финаме — адаптер на чтение, допуск, снимок, JWT, лимиты, окно обслуживания, соответствие инструментов.

### Modified Capabilities

- `broker-contour`: третий контур на брокере в порядке подъёма; плановая недоступность брокера при сверке; граница общего кода включает `src.finam`.
- `control-api`: ресурс `/api/v1/finam`, поле `broker` у контуров на брокере, значение `maintenance` у `account_link`.
- `configuration`: секция `[contours.finam]`.

## Impact

- **Код:**
  - новые `src/broker_contour/finam/`;
  - `src/broker_contour/contours.py`, `process.py`, `reconcile.py`, `port.py`;
  - `run.py`, `src/config.py`, `src/config_loader.py`;
  - `src/bot/account_control.py`, `control_views.py`, `comparison_view.py`;
  - `src/data/export_api.py`;
  - `src/notifier/templates/console_view.py`;
  - `run.spec`, `docs/api/openapi.yaml`.
- **Код Ф0** приходит merge-коммитом. PR этой ветки несёт и коммиты Ф0: Ф0 лучше влить в `release/release-12` раньше (после 30.11).
- **Окружение:** `FINAM_SECRET_TOKEN`, `FINAM_ACCOUNT_ID` (из Ф0), бот и канал Финама (имена — open question).
- **«Счёт», «Песочница», эмуляция:** поведение, пути, файлы и тексты не меняются.
- **Ветки:** `feature/finam-contour` от `release/release-12` без upstream; UI — своя ветка. Push и PR делает владелец.
