# Контракты API робота

| Файл | Что описывает |
|---|---|
| [openapi.yaml](openapi.yaml) | HTTP API робота, версия 2.3.0: точка входа, ядро, эмуляция, история, песочница, свечи для графиков, экспорт рыночных данных, отчёт кластеров |
| [market-events.asyncapi.yaml](market-events.asyncapi.yaml) | События рыночной структуры в Redis Stream |
| [v2-decisions.md](v2-decisions.md) | Решения API 2.0, таблица «было → станет», план «Счёта» и справочника |

Робот отдаёт `openapi.yaml` по `GET /openapi.yaml` и показывает Swagger UI на
`/docs` (по умолчанию `http://127.0.0.1:8101/docs`).

## Ключи

| Ключ | Переменная `.env` | Что открывает |
|---|---|---|
| `ControlToken` | `ROBOT_CONTROL_TOKEN` | `/api/v1/core`, `/api/v1/emulation…`, `/api/v1/history…`, `/api/v1/sandbox…`, `/api/v1/candles…`; только с `Host` 127.0.0.1, localhost или [::1] |
| `ExportToken` | `MARKET_DATA_EXPORT_TOKEN` | `/api/v1/market-data/…`, `/api/v1/clusters/report` |

Точка входа `GET /api/v1` принимает любой из двух ключей и показывает
разделы, доступные этому ключу. Ключ передаётся только заголовком
`Authorization: Bearer …`.

## Разделы

```mermaid
flowchart LR
    home["GET /api/v1"] --> core["/core"]
    home --> emu["/emulation"]
    home --> hist["/history"]
    home --> sbx["/sandbox"]
    home --> cnd["/candles — ряды свечей"]
    home --> md["/market-data/…"]
    home --> cl["/clusters/report"]
    emu --> target["/target — режим: on, no_entries, close_all"]
    emu --> members["/instruments — состав, /remove — убрать пачкой"]
    emu --> elog["/log"]
    hist --> run["/{run_id}"] --> stop["/stop"]
    run --> report["/report"]
    run --> rlog["/log"]
    sbx --> starget["/target — on, no_entries, close_all"]
    sbx --> bindings["/bindings — состав из привязок, /remove — убрать"]
    sbx --> rec["/reconcile — последний отчёт сверщика"]
    sbx --> slog["/log"]
    cnd --> series["/{instrument}/{timeframe} — свечи окна, _links.earlier — раньше"]
```

**Песочница (2.1.0, add-sandbox-contour).** Контур на брокере в песочнице
Т-Инвест. Состав — привязки (`id` из `[strategies.*]`), а не инструменты;
меняется командами и без запущенного процесса. На этапе С2 контур только
читает счёт и сверяет его с журналом: заявок нет, `confirm` не нужен.

**Песочница с заявками (2.2.0, add-tinvest-orders).** При
`[contours.sandbox] orders = true` контур торгует привязками настоящими
заявками в песочнице. Ядро само выбирает контракт привязки по правилам
эмуляции; если не может — `422 BINDINGS_REJECTED` с причиной. В `current`
появились `orders_enabled`, `trades` (открытые сделки), `orders_in_flight`
(незавершённые заявки) и `stream_link`. У привязки `current` — `warming`,
`trading`, `draining`, `closing`, `rejected` (с `reason`) или `watching`.
Снятие привязки с `no_entries` доводит её сделки до выхода, с `close_all` —
закрывает их.

**Свечи (2.3.0, add-candles-api).** Свечи рыночной базы ядра для графиков
UI — только чтение, без запросов к брокеру. `GET /api/v1/candles` — какие
ряды «инструмент × таймфрейм» хранятся; `GET /api/v1/candles/{instrument}/{timeframe}`
— последние `limit` свечей окна `[from, to)` (по умолчанию 500 до текущего
момента), листание назад — по `_links.earlier`. Имена — как в командах
контуров: `NG-12.26` или `SBER`; биржевой тикер и код семейства — `404` с
подсказкой в `suggestions`. Таймфреймы — только хранимые, без агрегации.
Пока ядро не открыло рыночную базу — `503 MARKET_DATA_NOT_READY`.

## Форматы

- Ответы управления — `application/json` с `_links` (куда перейти) и
  `_actions` (что сервер примет сейчас: метод, адрес, готовое тело).
- Ошибки управления — `application/problem+json` (RFC 9457) с полем `code`.
- Экспорт рыночных данных и отчёт кластеров — прежний формат 1.8, ошибки
  `{code, message}`: их читает Historical Broker API Emulator.
- Ревизия целевого состояния эмуляции — заголовок `ETag`; команда с
  `If-Match` по устаревшей ревизии получает `412 REVISION_CHANGED`.
