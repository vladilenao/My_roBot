## Context

После `entry-filter-admission` новый вход проходит одну точку допуска в обоих циклах:

```
TradingBot._analyze / LiveAnalysis._calculate
  └─ TradeManager.actions_for_signal
       ├─ сделка связки открыта → _manage_owned (противоположный сигнал, добор)
       └─ иначе → _plan_entry
            1. фильтр профиля привязки (filter.<профиль>) → недопуск filter-rejected
            2. текущая цена, направления, план профиля, портфельный допуск
```

Итог допуска уходит в события: план — `signal` (консольный блок ПЛАН), недопуск — `rejected` (блок ОТКЛОНЕНО под СИГНАЛ).

ClusterSource пишет в Redis Stream `cluster.events` (тот же Redis, что `market.events` робота) по контракту `cluster-source-events.asyncapi.yaml`, `schema_version = 1`. Поля записи — строки, `payload`, `reasons` и `baseline_days` — JSON-строки. Каждое событие несёт `event_id` (sha256, ключ дедупликации), `category`, `event_type`, `ticker` (ключ хранения ClusterSource, например `BR_CONTINUOUS`), `secid` (биржевой тикер, у непрерывного фьючерса — активный контракт), `timestamp` (время рынка) и `published_at` (Redis TIME перед `XADD`).

## Goals / Non-Goals

**Goals:**
- Для каждого кандидата нового входа привязки с включёнными «Кластерами» — один воспроизводимый вердикт, без заглядывания вперёд, одинаковый в live и в истории.
- Теневой режим: видеть в консоли, почему фильтр запретил или подтвердил бы вход, и получать отчёт для решения о боевом режиме.
- Отсутствие кластерных данных никогда не останавливает торговлю.

**Non-Goals:** см. proposal.

## Decisions

### D1. Потребитель `cluster.events` и хранилище

`ClusterEventsConsumer` — daemon-поток, как `PatternWorker`, создаётся в `run.py` только в live-режиме и только если хотя бы у одной привязки `clusters != "off"`. Подключение к Redis — параметры `[market_structure]` (`redis_host`, `redis_port`, `redis_db`) и пароль `MARKET_STRUCTURE_REDIS_PASSWORD`; отдельного секрета нет.

- При старте: `XGROUP CREATE cluster.events robot-v1 0 MKSTREAM` (ошибка `BUSYGROUP` — группа уже есть). Первая группа читает поток с начала, то есть получает всю историю, которую хранит Redis (90 суток).
- Сначала свои неподтверждённые (`XREADGROUP … 0`), затем новые (`>`), `BLOCK 5000`, `COUNT 500`.
- Запись в SQLite одной транзакцией на пачку, `INSERT OR IGNORE` по `event_id`; `XACK` — только после `COMMIT`. `NOACK` не используется.
- Сохраняются `category = signal`, `category = data_quality` и `event_type = signal_revoked`. `signal_confirmed` подтверждается без записи: для вердикта он не нужен.
- Ошибка Redis — повтор с паузой 1, 2, 4 … 60 с; поток не падает. Время последнего успешного чтения хранится в памяти и в таблице `consumer_state`.

`data/cluster-events.sqlite3` (WAL, `user_version = 1`):

```sql
cluster_events(event_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, category TEXT NOT NULL,
               event_type TEXT NOT NULL, secid TEXT NOT NULL, source_ticker TEXT NOT NULL,
               direction TEXT, signal_id TEXT, timestamp TEXT NOT NULL, published_at TEXT NOT NULL,
               fields_json TEXT NOT NULL, received_at TEXT NOT NULL)
INDEX (secid, published_at); INDEX (signal_id); INDEX (source_ticker, published_at)
consumer_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)   -- last_read_at, last_stream_id
```

Отдельный файл, а не `trades.sqlite3`: это внешние факты, а не торговое состояние; `trades.sqlite3` остаётся единственным источником истины по сделкам.

*Альтернатива:* читать Redis прямо в момент вердикта. Отклонена: допуск зависел бы от доступности Redis, а исторический режим не мог бы воспроизвести вердикт.

### D2. Исторический режим

Потребитель не запускается. Исторический прогон не меняет боевые файлы, поэтому работает со своим хранилищем в каталоге прогона: копия боевого `cluster-events.sqlite3` (открывается только на чтение, если он есть) плюс разовая догрузка потока от последнего сохранённого `stream_id` (`XRANGE` пачками, `INSERT OR IGNORE`, без группы и `XACK`). Redis недоступен — прогон идёт по скопированным событиям. Проверка отставания чтения (D5, шаг 0) в истории не применяется: событие учитывается только по `published_at ≤ T`.

### D3. Сопоставление инструмента

Событие относится к инструменту робота, если `secid` совпадает с биржевым тикером инструмента (`BRX6`, `SBER`). Если по инструменту событий нет:
- фьючерс, и за последние сутки есть события, у которых `source_ticker` без учёта регистра равен `<семейство>_CONTINUOUS` (`family_for` из `src/instruments/futures.py`), с другим `secid` → `unknown/contract_mismatch` с контрактом коллектора;
- иначе → `unknown/no_coverage`.

Псевдонимы тикеров (TCSG ↔ T) не вводятся: расхождение исправляется в ClusterSource.

### D4. Свечи

1m закрытые свечи инструмента берутся из кэша рыночных данных через провайдер кадров, как у `triple_screen` (`HtfFrameProvider.frame_for(instrument, "1m", max_close=T)`): в кадр попадают только свечи, закрывшиеся не позже `T`. Используются `datetime` (время открытия свечи), `high`, `low`, `close`. Для σ15 нужен 10-дневный хвост истории; при его нехватке вердикт — `unknown/no_sigma_history`.

### D5. Алгоритм вердикта (`clusters-v1`)

Входы: `T` — время закрытия бара сигнала (`decision.bar_time`); `s` = +1 для BUY, −1 для SELL; инструмент; пороги из `[clusters]`.

`close_at(t)` — `close` последней 1m свечи, у которой `время открытия + 1 мин ≤ t`.

0. **Есть ли данные.** По порядку, первый сработавший даёт `unknown`:
   - live и с последнего успешного чтения Redis прошло больше `max_consumer_lag_sec` → `consumer_lag`;
   - инструмент не сопоставлен (D3) → `no_coverage` / `contract_mismatch`;
   - последнее событие `category = data_quality` по `secid` с `published_at ≤ T` — `data_degraded` → `data_degraded`;
   - меньше `sigma_days` торговых дней 1m истории до дня `T` → `no_sigma_history`;
   - нет 1m свечи, закрывшейся не раньше чем за 15 минут до `T`, или свечи за 15 минут до `T` → `candles_missing`. Ошибка расчёта тоже даёт `unknown/candles_missing` и не ломает допуск.
1. **σ15.** Для каждой свечи t за `sigma_days` торговых дней до дня `T`, у которой в тот же день есть свеча на 15 минут раньше: `|ln(close_t / close_{t−15м})|`. σ15 — медиана.
2. **Ход до входа.** `pre = s · ln(close_at(T) / close_at(T − 15м)) / σ15`.
3. **Положение в диапазоне.** По свечам, открытым в [T − 120м, T − 1м]: `H = max(high)`, `L = min(low)`; `pos = (close_at(T) − L) / (H − L)` (при `H = L` — 0,5); по направлению входа `pos_s = pos` для BUY и `1 − pos` для SELL.
4. **События.** `category = signal`, тот же `secid`, `published_at` в (T − `initiative_window_min`, T], и нет `signal_revoked` с тем же `signal_id` и `published_at ≤ T`. Для каждого:
   - `d` = +1 для `direction = long`, −1 для `short`; `neutral` пропускается;
   - отношение: `same`, если `d = s`; `opposite`, если `d = −s`;
   - свой ход: `own = d · ln(close_at(published_at) / close_at(published_at − 15м)) / σ15`;
   - метка: `absorption` с `payload.absorbed_side` = стороне входа → `absorption_against`; иначе `own ≥ pre_move_veto_sigma` → `climax`; иначе `own < initiative_max_sigma` → `initiative`; иначе `other`.
5. **Запрет.** `pre ≥ pre_move_veto_sigma`, и среди событий не старше `climax_window_min` есть `climax` с `same`, или `absorption_against`, или `initiative` с `opposite` → `veto`; если ещё `pos_s ≥ extreme_position` → `veto_strong`. Причина `climax`.
6. **Подтверждение.** Иначе, если есть `initiative` с `same` от `large_aggressor` или `stacked_imbalance` → `confirm`, причина `initiative`.
7. Иначе `neutral`, причина `no_evidence`.

Значения по умолчанию: `pre_move_veto_sigma = 1.5`, `initiative_max_sigma = 0.5`, `extreme_position = 0.8`, `climax_window_min = 30`, `initiative_window_min = 60`, `sigma_days = 10`, `max_consumer_lag_sec = 60`.

**Пример (BR-11.26, SELL, 15.10, T = 13:50 UTC / 16:50 МСК).** σ15 = 12,82 б.п. (8 831 значение за 05.10–14.10). `close_at(13:50)` = 104,63 (свеча 13:49), `close_at(13:35)` = 104,92 → `pre` = +2,16σ. H = 105,20, L = 104,06 → `pos` = 0,50, `pos_s` = 0,50. События:

| `published_at` | `event_type` | `direction` | Отношение | `own` | Метка |
|---|---|---|---|---|---|
| 13:23:34 | large_aggressor | long | opposite | +3,27σ | climax |
| 13:25:01 | stacked_imbalance | long | opposite | +3,27σ | climax |
| 13:37:27 | large_aggressor | short | same | +0,07σ | initiative |
| 13:40:00 | volume_spike | short | same | +2,45σ | **climax** |

`pre` ≥ 1,5 и есть `climax`/`same` 10 минут назад → `veto`; `pos_s` < 0,8 — не сильный. Факт: стоп, −1 455,94 ₽; через 15 минут −39,11 б.п., через 60 минут −74,27 б.п. по направлению сигнала.

### D6. Место в допуске

В `_plan_entry` до фильтра профиля:

```
_plan_entry
  0. clusters != off → вердикт (D5), трасса filter.cluster, вердикт кладётся в SignalAdmission.cluster
  1. фильтр профиля привязки → filter-rejected (вердикт остаётся в недопуске)
  2. enforce и вердикт ≥ enforce_level → недопуск cluster-climax
  3. текущая цена, план, портфельный допуск (вердикт уходит с планом или недопуском)
```

Вердикт считается до фильтра профиля, чтобы отчёт видел все входные сигналы привязки, а не только прошедшие фильтр тренда. Решение о входе от этого не меняется: в `shadow` шаг 2 пропускается.

`enforce_level = "veto"` запрещает при `veto` и `veto_strong`; `"veto_strong"` — только при `veto_strong`.

Сигналы по открытой сделке связки (`_manage_owned`) и доборы вердикт не получают.

*Альтернатива:* считать вердикт в циклах робота при создании кандидата. Отклонена: циклов два (`TradingBot`, `LiveAnalysis`), а допуск уже собран в `_plan_entry`; строка «Кластеры» печатается в блоке ПЛАН/ОТКЛОНЕНО, который и так идёт после допуска.

*Альтернатива:* сделать «Кластеры» профилем фильтра. Отклонена: у привязки ровно один `filter`, а тренд и лента сделок — независимые проверки, нужны обе.

### D7. Вердикт в событиях и консоли

`SignalAdmission.cluster: ClusterVerdict | None`. `TradingBot` и `LiveAnalysis` передают его в события `signal` и `rejected` необязательным ключом `clusters = {"verdict", "reason", "message"}`; ключ добавляется в необязательные поля этих типов в `src/events/schema.py`. Telegram ключ не использует.

Консоль (`console_view._plan`, `_rejected`) печатает первой строкой деталей `Кластеры: <message>` для `veto_strong`, `veto`, `confirm`, `unknown`; для `neutral` строки нет. Текст строится из входов трассы, время — МСК, инструмент — короткое имя:

| Вердикт | `message` (формат; числа иллюстративные, кроме первой строки — это D5) |
|---|---|
| shadow, veto | `запретил бы — вход на кульминации · ход до входа +2,16σ (порог 1,5σ) · середина 2ч диапазона · 16:40 всплеск продаж ×9,6 после хода +2,45σ` |
| shadow, veto_strong | `запретил бы — вход на кульминации у края 2ч диапазона (96%) · …` |
| confirm | `подтверждает — инициатива до движения · 12:50 и 13:03 крупные покупки, пока цена падала (−2,26σ / −1,45σ)` |
| unknown | `нет данных — коллектор ведёт NG-10.26, робот торгует NG-11.26` |
| enforce, veto | недопуск `cluster-climax` с причиной `Кластеры: вход на кульминации`, детали — та же строка без «запретил бы» |

Событие-улика в тексте — самое свежее из учтённых: время публикации, вид (`всплеск покупок/продаж ×ratio`, `крупная покупка/продажа <size> <size_unit>`, `лестница покупок/продаж`, `поглощение покупок/продаж`) и свой ход.

### D8. Трасса `filter.cluster`

`CalculationTrace` с `algorithm = "filter.cluster"`, `algorithm_version = "clusters-v1"`, ссылками `assignment_id` и `signal_id`. Входы: инструмент, таймфрейм, сторона, `T`, режим, σ15, `sigma_days`, `pre`, `pos_s`, `close_at(T)`, `close_at(T−15м)` с временем свечей, H, L, пороги, список событий (`event_id`, `event_type`, `direction`, отношение, `published_at`, `own`, метка). Выход: вердикт, причина, `message`. Исход `REJECTED` — только когда вердикт привёл к недопуску (`enforce`); иначе `ACCEPTED`.

Трасса пишется в `calculations` `trades.sqlite3` и `trade_decision_trace.log`; схема не меняется. Это и есть хранилище вердиктов для отчёта.

### D9. Отчёт `GET /api/v1/clusters/report`

Маршрут на существующем локальном HTTP-сервере (`MarketDataExportServer`): тот же выключатель `export_enabled`, хост, порт и токен. Контракт — тег `Clusters` в `docs/openapi.yaml`.

- Период: `period` календарных дней по МСК-дате `T`, заканчивая `date` включительно.
- Сигналы периода — трассы `filter.cluster` из `calculations`; по `signal_id` — сделка (`trades.signal_id`), её исполнения и чистый итог (`positions.net_realized_pnl`, `null` у открытой).
- Движение цены через 15 и 60 минут: `s · ln(close_at(T + h) / close_at(T)) · 10 000` по 1m свечам локального журнала; горизонт не наступил — `pending`, свечи нет — `no_data`.
- Группы по вердикту, `veto_effect` по `includes = [veto_strong, veto]`, список сигналов по возрастанию `T`.
- `_links.self`, `_links.previous` — период той же длины перед `from`; `_links.next` — после `to`, если он не целиком в будущем.
- Отчёт считается на запрос, без кэша; при отсутствии сигналов — пустые группы и список, не 404.

### D10. Единый OpenAPI

`docs/market-data/openapi.yaml` перенесён в `docs/openapi.yaml`; `export_api._OPENAPI` указывает на него. Новые HTTP-сервисы робота описываются только там, отдельным тегом.

## Risks / Trade-offs

- **Повторные сигналы.** Пока сделки нет, `macd_rsi_stoch` может давать BUY несколько баров подряд; каждый получит вердикт и войдёт в отчёт. Отчёт показывает это честно; дедупликация — после первых данных.
- **σ коллектора и робота различаются.** ClusterSource считает по тикам, робот — по своим 1m свечам; для 15.10 BR-11.26 это +2,6σ против +2,16σ. Правило живёт в роботе и использует только его свечи.
- **Часы.** `published_at` — время Redis, `T` — время робота; оба на одной машине, расхождение — миллисекунды.
- **Первый запуск** читает до 90 суток потока (сейчас ~27 тыс. событий, единицы мегабайт).
- **Сервер отчёта** — тот же локальный API, что экспорт рыночных данных. `export_enabled = true` по умолчанию; без `MARKET_DATA_EXPORT_TOKEN` в окружении API не запускается (предупреждение в консоли и логе), робот работает дальше.
- **Подгонка.** Правило уточнялось на 7 сделках 15.10; это не проверка. Проверка — теневой режим и отчёт за ≥ 20 торговых дней.

## Migration Plan

1. Выпуск: в `default.toml` `clusters = "shadow"` на всех 37 привязках `macd_rsi_stoch` — решения о входе не меняются; у остальных привязок ключа нет (`off`). Локальный API включён по умолчанию.
2. Пользователь задаёт `MARKET_DATA_EXPORT_TOKEN` в `.env`, чтобы вызывать отчёт.
3. Через ≥ 20 торговых дней — отчёт `period = 30`; `enforce` включается пользователем по привязкам, где `veto_effect.net_effect > 0` и группа `veto` хуже `neutral` по движению через 60 минут.
4. Откат — `clusters = "off"`; хранилище и трассы не мешают.
