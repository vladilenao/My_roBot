# Схемы payload событий шины

Форма каждого события закреплена здесь и проверяется тестом
`tests/unit/events/test_event_schemas.py`: код, документ и настоящие события обязаны
совпадать, иначе сборка падает. Причина — молчаливая рассогласованность, когда канал
читает поле, которого в событии уже нет.

Общая форма события:

```json
{
  "type": "signal",
  "instrument": "NG-10.26",
  "bar_time": "2026-09-26T19:00:00+00:00",
  "timeframe": "1h",
  "payload": {
    "side": "SELL",
    "quantity": 7,
    "entry": "130.5",
    "stop": "132",
    "targets": ["128", "126"],
    "expected_r": "2.33"
  }
}
```

- `type` — один из 19 типов каталога ([events.md](events.md)).
- `instrument` — короткое имя контракта; пустая строка, если событие не про контракт.
- `bar_time` — время закрытия бара в ISO8601 или `null`.
- `timeframe` — таймфрейм сигнала или пустая строка.
- `payload` — объект, описанный ниже; `Decimal` сериализуется строкой, `datetime` — ISO8601.

## Как читать схемы

Схема описывает только `payload` — оболочка события одинакова для всех типов.
`required` — поля, которые канал вправе читать всегда; остальные приходят по факту
и могут отсутствовать. `additionalProperties: false` означает, что поле, которого нет
в схеме, — ошибка отправителя, а не повод молча его игнорировать.

`Decimal` сериализуется строкой, поэтому `price`, `entry`, `stop`, `fee` и `expected_r`
в JSON имеют строковый тип: так не теряются знаки и точность. `quantity`, `tick_count`
и `error_count` — числа, `filtered_out` — булево, `targets` — массив строк с ценами.

Блоки ниже перепечатывают `src/events/schema.py`: менять форму события нужно там, а
документ и тест подтянутся проверкой.

## События анализа

Публикует бот по итогам тика: решение каждой стратегии, рекомендация, прошедшая допуск,
и отказ на допуске.

### `decision`

Исход анализа по контракту за тик. `outcome` — `signal_buy`, `signal_sell`, `no_signal`
или `filtered`; при `filtered` в `filtered_out` лежит `true`, а `side` — сторона,
которую отвергли. Показывается только консоли: пользователю не нужен шум по каждой
стратегии.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload decision",
  "type": "object",
  "required": [
    "outcome",
    "side"
  ],
  "properties": {
    "outcome": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "price": {
      "type": "string"
    },
    "strategy": {
      "type": "string"
    },
    "filter_profile": {
      "type": "string"
    },
    "filtered_out": {
      "type": "boolean"
    },
    "event_id": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `signal`

Рекомендация, прошедшая допуск. `targets` — массив строк с ценами целей по возрастанию
риска, `expected_r` — ожидаемый результат в долях риска с точностью до сотых. Консоль и
Telegram.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload signal",
  "type": "object",
  "required": [
    "side",
    "quantity",
    "entry",
    "stop"
  ],
  "properties": {
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "entry": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "targets": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "expected_r": {
      "type": "string"
    },
    "strategy": {
      "type": "string"
    },
    "filter_profile": {
      "type": "string"
    },
    "trade_id": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `rejected`

Сделка не допущена к исполнению. `reason` — причина из риск-менеджера, `code` — машинный
код, если риск-менеджер его вернул.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload rejected",
  "type": "object",
  "required": [
    "reason"
  ],
  "properties": {
    "reason": {
      "type": "string"
    },
    "code": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "price": {
      "type": "string"
    },
    "strategy": {
      "type": "string"
    },
    "filter_profile": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `heartbeat`

Пульс робота по окончании тика: сколько тиков отработано и сколько из них завершилось
ошибкой.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload heartbeat",
  "type": "object",
  "required": [
    "tick_count",
    "error_count"
  ],
  "properties": {
    "tick_count": {
      "type": "number"
    },
    "error_count": {
      "type": "number"
    }
  },
  "additionalProperties": false
}
```

### `error`

Ошибка операции. `operation` — человекочитаемое название операции с коротким именем
контракта, технические детали остаются в `bot_debug.log`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload error",
  "type": "object",
  "required": [
    "operation"
  ],
  "properties": {
    "operation": {
      "type": "string"
    },
    "message": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

## События брокера

Публикует брокер после подтверждённого исхода: только он знает PnL, баланс и
идентификатор заявки, поэтому события исполнения не идут из журнала состояния — иначе
строка задвоилась бы. У всех типов обязателен `trade_id`; пустой значит, что событие не
относится к сделке. Остальные поля приходят по факту: `order_id` и `execution_id` —
идентификаторы заявки и исполнения, `status` — состояние сделки у брокера, `quantity` и
`price` — объём и цена, `fee` — комиссия, `reason` — код отказа, `message` — текст для
пользователя, `broker_type` — исходный тип события брокера, `occurred_at` — время в
ISO8601.

### `order_accepted`

Брокер принял заявку. Далее придёт `trade_opened` или `order_rejected`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload order_accepted",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `order_rejected`

Брокер отклонил заявку, сделка не открыта.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload order_rejected",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `trade_opened`

Основная часть сделки исполнена: усреднение и потери ещё возможны.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload trade_opened",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `position_added`

Добавление к позиции по новому сигналу.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload position_added",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `stop_hit`

Сработала защита.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload stop_hit",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `target_hit`

Закрыта часть позиции по цели.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload target_hit",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `trade_closed`

Позиция закрыта полностью.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload trade_closed",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `trade_cancelled`

Сделка снята до исполнения.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload trade_cancelled",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

## События защиты и риска

### `protection_armed`

Защитные ордера выставлены.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload protection_armed",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `stop_moved`

Защитный стоп перенесён.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload stop_moved",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `reservation_changed`

Изменилась резервация риска и маржи под сделку.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload reservation_changed",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

### `risk_limit_hit`

Вход отбит лимитом риска.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload risk_limit_hit",
  "type": "object",
  "required": [
    "trade_id"
  ],
  "properties": {
    "trade_id": {
      "type": "string"
    },
    "side": {
      "type": "string"
    },
    "quantity": {
      "type": "number"
    },
    "price": {
      "type": "string"
    },
    "stop": {
      "type": "string"
    },
    "take_profit": {
      "type": "string"
    },
    "pnl": {
      "type": "string"
    },
    "fee": {
      "type": "string"
    },
    "order_id": {
      "type": "string"
    },
    "execution_id": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    },
    "occurred_at": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```

## Служебные события

Служебные события не относятся к конкретной сделке: у них пустой `trade_id`.

### `clearing_done`

Сверка счёта завершена, расхождений нет.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload clearing_done",
  "type": "object",
  "required": [],
  "properties": {
    "balance": {
      "type": "string"
    },
    "positions": {
      "type": "number"
    }
  },
  "additionalProperties": false
}
```

### `rate_limited`

Исчерпан лимит запросов к брокеру. Пользователю не показывается.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "payload rate_limited",
  "type": "object",
  "required": [],
  "properties": {
    "source": {
      "type": "string"
    }
  },
  "additionalProperties": false
}
```
