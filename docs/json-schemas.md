# JSON-схемы полей базы

Поля-документы (`*_json`) в SQLite объявлены как `TEXT NOT NULL` и хранят
строго сериализованный JSON. Форму каждого поля задаёт единственный
производитель в коде; ниже — формальная структура, производитель и пример.
Общая ER-схема и типы колонок — в [database.md](database.md).

| Таблица | Колонка | Производитель | Раздел |
|---|---|---|---|
| `trades` | `plan_json` | `manager._plan_payload` | [plan_json](#plan_json) |
| `trades` | `profile_json` | `manager._profile_payload` | [profile_json](#profile_json) |
| `trades` | `profile_state_json` | `manager._save_profile_result` | [profile_state_json](#profile_state_json) |
| `outbox` | `payload_json` | `manager._action_payload` / `reducer._ensure_pv_order` | [payload_json (outbox)](#payload_json-outbox) |
| `events` | `payload_json` | `reducer._payload` | [payload_json (events)](#payload_json-events) |
| `market_inputs` | `data_json` | `audit.MarketInput.values` | [data_json](#data_json-market_inputs) |
| `market_inputs` | `seed_json` | `audit.MarketInput.seed` | [seed_json](#seed_json-market_inputs) |
| `calculations` | `input_json` | `audit.CalculationTrace.inputs` | [input_json](#input_json) |
| `calculations` | `steps_json` | `audit.CalculationTrace.steps` | [steps_json](#steps_json) |
| `calculations` | `rounding_json` | `audit.CalculationTrace.rounding` | [rounding_json](#rounding_json) |
| `calculations` | `output_json` | `audit.CalculationTrace.result` | [output_json](#output_json) |

## Общие соглашения

- Цены, деньги и прочие `Decimal` сериализуются **строками**, чтобы не терять
  точность (хелперы `_json_value`, `_factor_text`, `_text`).
- `datetime` → строка ISO8601 (`value.isoformat()`).
- Ключи JSON нормализуются через `sort_keys=True`; аудит `_dump` дополнительно
  использует компактные `separators=(",", ":")`. Неизменяемые payload одного
  производителя сериализуются устойчиво, а не переписываются сегодняшней конфигурацией.
- «Измеренная» величина — объект `{"value": <любое>, "unit": <строка>}`;
  `unit` обязателен. `value` может быть числом, строкой, объектом или массивом.
- Записи неизменяемы и идемпотентны (`ON CONFLICT ... DO NOTHING`): повторная
  обработка того же события не меняет JSON.
- В аудит-трассы секреты не пишутся.

## Область 1. Журнал сделок

### plan_json

`trades.plan_json` (`TEXT NOT NULL`). Снимок плана сделки на момент создания.
Производитель: `src/trade_management/manager.py::_plan_payload`.

```json
{
  "reference_entry": "строка — цена входа по плану",
  "stop_price":       "строка — первоначальный стоп",
  "targets": [
    { "target_id": "строка — обозначение цели", "share": "строка — доля объёма",
      "price": "строка — исходная цена", "initial_step": "строка — исходное расстояние",
      "price_basis": "r | absolute" }
  ],
  "timeframe": "строка — таймфрейм сопровождения",
  "stop_basis": "structural | atr-floor | atr-cap",
  "algorithm_version": "legacy-v1 | economics-v2",
  "entry_order_type": "market | limit",
  "requested_quantity": "целое или null — явный запрос",
  "price_step": "строка или null — сохранённая сетка цены",
  "cost_snapshot": {"commission": "строка — ₽/контракт/сторону", "slippage": "строка — ₽/контракт/круг"},
  "admission_snapshot": "объект — эффективные пороги и политика исходного допуска",
  "economics": "объект денежной экономики или null"
}
```

Пример:

```json
{"reference_entry":"100","stop_price":"96","targets":[{"target_id":"tp-1","share":"0.5","price":"106","initial_step":"6","price_basis":"r"},{"target_id":"tp-2","share":"0.5","price":"110","initial_step":"10","price_basis":"r"}],"timeframe":"15m","stop_basis":"structural","algorithm_version":"economics-v2","entry_order_type":"limit","requested_quantity":null,"price_step":"1","cost_snapshot":{"commission":"1.5","slippage":"1"},"admission_snapshot":{"portfolio_pct":"2","go_per_contract":"8000","min_trade_risk_pct":"0","min_risk_cost_ratio":"2","min_net_payoff":"1.5","max_slippage_r":"0.25"},"economics":{"quantity":10,"risk_amount":"80","reward_amount":"160","costs_amount":"40","payoff_ratio":"1.5","fixed_reward_amount":"160","fixed_quantity":10,"target_quantities":{"tp-1":5,"tp-2":5},"slippage_amount":"10"}}
```

Читается при восстановлении (`storage._load_trade`) и при привязке объёма целей
защитного закрытия (`reducer._bind_target_plan`, `share` → `Decimal`).
В примере сохранённые факторы `trades.price_step=1`, `trades.step_cost=2`, то есть V=2.
R=80 ₽, C=40 ₽, gross=160 ₽, net-план=120 ₽, payoff=1.5.
`economics.target_quantities` — целые аллокации, `fixed_quantity`/`fixed_reward_amount`
— известная фиксируемая часть. При неизвестном полном исходе тренда/плана без
целей `reward_amount` и `payoff_ratio` — null; `expected_r` вычисляется из
экономики, а не хранится отдельным обещанием доходности.
Отсутствующая версия означает legacy-v1; новые планы требуют cost snapshot и
LIMIT. Старые отсутствующие поля допустимы и не переписываются при восстановлении.
Фактические цены целей/защиты, D0 и Qmax хранятся отдельно, не заменяют plan_json.
Процент в admission_snapshot — аудит исходного допуска; текущий глобальный бюджет
проверяется заново для всех открытых эпох.

### profile_json

`trades.profile_json` (`TEXT NOT NULL`). Снимок профиля сопровождения сделки.
Производитель: `src/trade_management/manager.py::_profile_payload`.

```json
{
  "name":       "строка — имя профиля (levels_rr, atr_trend, ...)",
  "version":    "строка — версия профиля",
  "parameters": "объект — параметры профиля из конфигурации"
}
```

Пример (профиль `levels_rr`):

```json
{"name":"levels_rr","version":"1","parameters":{"buffer_ticks":1,"target_R":[1,2],"shares":[0.5,0.5]}}
```

Состав `parameters` зависит от профиля — см.
[docs/trade-management/profiles.md](trade-management/profiles.md); значения
`Decimal` → строки.

### profile_state_json

`trades.profile_state_json` (`TEXT NOT NULL DEFAULT '{}'`). Состояние профиля
сделки (переносится через рестарт). Начальное значение `{}`; профильные результаты
сохраняет `manager._save_profile_result`, включая активацию без target fill:

```json
{"trailing_active":true,"adds_disabled":true,"trailing_extreme":"108"}
```

Поля зависят от профиля; у levels_rr/pattern_targets `be_skip_reason` (например,
`below-min-be-r`) — диагностический пропуск, не исполнение.
Число подтверждённых доборов восстанавливается по distinct ADD-ордерам с fill
и сохранённому legacy-счётчику; partial fill одного ордера не считается новым добором.
Фактическая первоначальная мера/максимальный объём — в `trade_measurements`,
подтверждённые стопы и цели — в соответствующих таблицах.

## Область 2. Исполнение и команды брокеру

### payload_json (outbox)

`outbox.payload_json` (`TEXT NOT NULL`). Тело команды. Два вида:

**1. Торговое действие** — для команд менеджера. Производитель:
`src/trade_management/manager.py::_action_payload` (поля
`src/trade_management/actions.py`, `Decimal` → строки).

```json
{
  "command_id":     "строка",
  "trade_id":       "строка",
  "state_revision": "целое",
  "reason":         "строка",
  "type":           "OpenTrade | AddToTrade | ReduceTrade | CloseTrade | MoveStop | CancelEntry"
}
```

Дополнительные поля по типу действия:

| `type` | Поля |
|---|---|
| `OpenTrade` | `quantity` (целое), для LIMIT `order_type="limit"`, `limit_price` (строка) |
| `AddToTrade` | `quantity` (целое), собственные `order_type="limit"`, `limit_price` нового сигнала; после sizing `requested_quantity` и `limiting_constraint` для аудита/уведомления |
| `ReduceTrade` | `quantity` (целое), `target_id` (строка или `null`), `reference_price` решения рыночного выхода при известности |
| `MoveStop` | `stop_price` (строка) |
| `CloseTrade` | `reference_price` решения рыночного выхода при известности |
| `CancelEntry` | — |

Пример (`OpenTrade`):

```json
{"command_id":"cmd-1","trade_id":"t-1","quantity":2,"reason":"planned","state_revision":0,"type":"OpenTrade","order_type":"limit","limit_price":"100"}
```

У сохранённого legacy-payload без order_type остаётся market. ACK/PARTIAL-pending
восстанавливает собственные лимит, незаполненный объём и время постановки; новая
средняя не заменяет лимит уже принятой заявки.

**2. Защитное закрытие брокера** — синтезированная команда (маркер `:pv:` в
`command_id`) без собственного тела. Производитель:
`src/trade_journal/reducer.py::_ensure_pv_order`; значение:

```json
{}
```

### payload_json (events)

`events.payload_json` (`TEXT NOT NULL`). Данные события исполнения; колонка
`events.event_type` содержит значение `status` из этого же payload.
Производитель: `src/trade_journal/reducer.py::_payload`.

```json
{
  "fee":      "строка или null — комиссия",
  "price":    "строка или null — цена",
  "reason":   "строка или null — причина",
  "status":   "fill | partial | ack | reject | cancel",
  "quantity": "целое — исполненный объём",
  "low":      "строка или null — минимум рынка за окно",
  "high":     "строка или null — максимум рынка за окно",
  "fee_source": "broker | configured | unknown",
  "reference_price": "строка или null — опорная цена",
  "reference_kind": "строка или null — вид опоры",
  "order_side": "BUY | SELL | null",
  "slippage_amount": "строка или null — signed cash deviation",
  "slippage_source": "broker | simulated | null"
}
```

Пример (`fill`):

```json
{"fee":"3","fee_source":"configured","high":null,"low":null,"price":"9010","quantity":2,"reason":"protective","status":"fill","reference_price":"9000","reference_kind":"stop","order_side":"BUY","slippage_amount":"16.8","slippage_source":"simulated"}
```

Для примера шаг 10 и стоимость шага 8.4 ₽ дают deviation=16.8 ₽; оно уже
учтено actual price и вторично не списывается. `low/high` — исходный диапазон
fill-бара, не доказательство владения всем баром; durable holding observations
хранятся отдельно. Позднее уточнение имеет `event_type=FEE_ADJUSTMENT` и payload
`{"execution_id":"e-1","new_fee":"2","delta":"-1"}`; отдельный устойчивый
adjustment_id адресует исходный fill. Gross/quantity не меняются.

## Область 4. Аналитика стратегий

### data_json (market_inputs)

`market_inputs.data_json` (`TEXT NOT NULL`). Неизменяемое окно входных данных
расчёта. Производитель: `src/trade_management/audit.py::MarketInput.values`
(сериализация `_dump`).

```json
{
  "<имя величины>": { "value": "число | строка | объект", "unit": "единица" }
}
```

Пример:

```json
{"signal_price":{"value":100.0,"unit":"price"},
 "market":{"value":{"entry":100,"price_step":1,"resistance":103,"support":97},"unit":"local-market-inputs"},
 "parameters":{"value":{"buffer_ticks":1,"target_R":[1,2],"shares":[0.5,0.5]},"unit":"profile-parameters"}}
```

### seed_json (market_inputs)

`market_inputs.seed_json` (`TEXT`, NULLABLE). Семя рекуррентных расчётов
(состояние индикатора) того же окна. Производитель:
`audit.MarketInput.seed`; структура та же, что у `data_json`. Пустое семя
сохраняется как `{}`; `NULL` возможен только в исторических строках БД.

## Область 5. Аудит и экспорт

Трасса расчёта (`CalculationTrace`) пишется в `calculations` и повторно
выводится в `trade_audit.log` (JSONL) — см.
[docs/trade-management/storage-and-audit.md](trade-management/storage-and-audit.md)
§3–4.

### input_json

`calculations.input_json` (`TEXT NOT NULL`). Входы расчёта:
`audit.CalculationTrace.inputs` (`Mapping[str, MeasuredValue]`). Структура — как
у [data_json](#data_json-market_inputs).

### steps_json

`calculations.steps_json` (`TEXT NOT NULL`). Шаги вычисления
(`audit.FormulaStep`), минимум один:

```json
[
  {
    "formula":  "строка — формула шага",
    "operands": { "<имя>": { "value": "...", "unit": "..." } },
    "result":   { "value": "...", "unit": "..." }
  }
]
```

Пример:

```json
[{"formula":"floor(risk_budget / per_contract_risk)","operands":{"risk_budget":{"value":"1000","unit":"RUB"},"per_contract_risk":{"value":"420","unit":"RUB/contract"}},"result":{"value":2,"unit":"contracts"}}]
```

### rounding_json

`calculations.rounding_json` (`TEXT NOT NULL`). Правила округления (`Rounding`),
минимум один элемент — «no rounding»:

```json
[
  {
    "rule":      "строка — правило",
    "unrounded": { "value": "...", "unit": "..." },
    "rounded":   { "value": "...", "unit": "..." }
  }
]
```

Единицы `unrounded` и `rounded` всегда совпадают.

### output_json

`calculations.output_json` (`TEXT NOT NULL`). Результат расчёта — `MeasuredValue`:

```json
{ "value": "число | строка | объект", "unit": "строка" }
```

Примеры: `{"value":2,"unit":"contracts"}`,
`{"value":{"entry":"100.0","stop":"96","targets":[["tp-1","104","0.5"],["tp-2","108","0.5"]]},"unit":"trade-plan"}`.

## Связанная документация

- [database.md](database.md) — ER-схема по областям, типы и ограничения колонок.
- [docs/trade-management/profiles.md](trade-management/profiles.md) — параметры
  профилей (`profile_json`), четыре профиля сопровождения.
- [docs/trade-management/storage-and-audit.md](trade-management/storage-and-audit.md)
  §3–4 — аудит-трассы, `trade_audit.log` (JSONL), примеры живых расчётов.
- [docs/trade-management/trade-event.md](trade-management/trade-event.md) — CSV-проекция
  событий (читаемая форма `events.payload_json`).
