## Why

Финам подключается вторым реальным брокером из-за комиссий Т-Инвест. Т-Инвест не заменяется: оба брокера работают одновременно, свечи и анализ остаются у ядра на Т-Инвест, Финам только исполняет. Этапы Ф0–Ф5; это **Ф2 — контур `finam` только на чтение**.

Ф1 (`refactor-broker-port`) вынес брокера за порты, Ф0 (`add-finam-probe`) разведал Finam Trade API. Теперь нужен второй живой контур вида «счёт» на Финаме, который видит счёт, сверяется и показывается в API и UI без заявок. Так проверяется путь Финама до денег: допуск, JWT, лимиты, окно обслуживания, снимок, сверка.

## What Changes

```
 ядро (TINKOFF_TOKEN: свечи, анализ, карточки)
   ├─ account  → Т-Инвест   data/contours/account/  СЧЁТ          (без изменений)
   ├─ finam    → Финам      data/contours/finam/    СЧЁТ ФИНАМ    (новый, только чтение)
   └─ sandbox  → Т-Инвест   data/contours/sandbox/  ПЕСОЧНИЦА     (без изменений)
```

- **Заглушки proto из Ф0** — merge-коммитом `feature/finam-probe`. Разведку адаптер не импортирует.
- **Адаптер `src/broker_contour/finam/`** на чтение:
  - свой транспорт: один источник JWT (стрим `SubscribeJwtRenewal`, запасной `Auth`), повтор по `reset_time` из `GetUsageMetrics`, окно 05:00–06:15 МСК → `BrokerOutage`;
  - снимок из `GetAccount` и `GetOrders`;
  - соответствие `symbol` ↔ карточка ядра; ГО из `GetAssetParams` при каждом чтении.
- **Порты (развитие Ф1):**
  - `BrokerSession` делится на чтение и `TradingSession`;
  - новый порт `InstrumentCatalog` — карточки ядра; его собирает `run.py` из Т-Инвест и передаёт адаптеру Финама;
  - `BrokerOutage` — плановая недоступность брокера.
- **Общий код без веток по брокеру:**
  - чтение счёта в процессе сведено в один метод с исходом «снимок / потерян / окно»;
  - ключи `[contours.<номер>]` и секреты лога выводятся из описаний контуров;
  - `orders = true` отклоняется у описания без торговли.
- **Допуск:** токен видит ровно один счёт `FINAM_ACCOUNT_ID`; права по `TokenDetails.readonly`; отпечаток `broker = finam`; отказ — тревогой в канал.
- **Сборка:** описание `finam` (`provider = finam`, `trading = false`), свой надзиратель, `[contours.finam]`, свой бот и канал, заголовок «СЧЁТ ФИНАМ», `broker` в `status.json`.
- **API `/api/v1/finam/*`** — тот же ресурс, что `/api/v1/account/*`, собранный с другими частями:
  - команды состава — `409 ACCOUNT_READ_ONLY`;
  - факты и сверка Финама — общие поля, без полей Т-Инвест;
  - `GET /api/v1` — ключ `finam`; `broker` у `account` и `finam`;
  - `account_link` — плюс `maintenance`;
  - OpenAPI 2.14.0, только добавления.
- **UI** (отдельный репозиторий): страница «Счёт Финам», карточка в «Обзоре», строка в «Аварийной остановке», метка брокера.

**Не входит:** заявки, стопы, комиссии, поток заявок — Ф3; предохранители, сравнение, перенос привязок — Ф4.

## Capabilities

### New Capabilities

- `finam-contour`: контур `finam` на Финаме — транспорт, JWT, лимит, окно обслуживания, допуск, снимок, соответствие инструментов.

### Modified Capabilities

- `broker-contour`: описание контура (`provider`, `trading`, порядок подъёма), порты `TradingSession` и `InstrumentCatalog`, плановая недоступность брокера, граница общего кода.
- `control-api`: ресурс `/api/v1/finam`, `broker` у контуров на брокере, `account_link: maintenance`.
- `configuration`: `[contours.finam]`; ключи секций контуров на брокере — по описанию; `orders = true` у контура без торговли.

## Impact

- **Код:**
  - новые `src/broker_contour/finam/`, `src/broker_contour/tinvest/catalog.py`;
  - `src/broker_contour/port.py`, `contours.py`, `process.py`, `reconcile.py`, `admission.py`;
  - `run.py`, `src/config.py`, `src/config_loader.py`, `default.toml`;
  - `src/bot/account_control.py`, `control_views.py`, `comparison_view.py`, `src/data/export_api.py`;
  - `src/notifier/templates/console_view.py`;
  - `run.spec`, `docs/api/openapi.yaml`.
- **Ф0** приходит merge-коммитом ради заглушек; PR Ф0 в `release/release-12` лучше раньше Ф2.
- **Окружение:** `FINAM_SECRET_TOKEN`, `FINAM_ACCOUNT_ID` (из Ф0), бот и канал Финама.
- **«Счёт», «Песочница», эмуляция:** поведение, пути, ключи, файлы и тексты не меняются; добавлено только поле `broker`.
- **Ветки:** `feature/finam-contour` от `release/release-12` без upstream; UI — своя ветка. Push и PR делает владелец.
