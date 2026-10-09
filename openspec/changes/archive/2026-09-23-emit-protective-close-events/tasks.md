# Осмысленный план задач

Выполнение по согласованию дизайна: сначала брокер (D1), затем редьюсер (D2),
затем восстановление защиты (D3), затем тесты и проверки.

## 1. Журнальный брокер: защитные закрытия становятся событиями (D1)

- [x] 1.1 В `_process_addressed_stop` (`src/broker/journal_broker.py`) после снятия
      позиции в in-memory `PositionManager` формировать `ExecutionEvent`:
      command_id=`f"{trade_id}:pv:stop:{ключ_бара}"`, execution_id=`f"{command_id}:fill"`,
      order_id=command_id, status=`FILL`, filled_quantity=закрытый остаток,
      price=цена стопа либо гэп-open (хуже стопа), reason=`protective`, fee=0,
      time=время бара; добавлять событие в `_addressed_events`
- [x] 1.2 В целевом цикле (строки 599–611) на каждое исполнение цели формировать
      аналогичное событие c command_id=`f"{trade_id}:pv:tp:{target_id}:{ключ_бара}"`
      и reason=`tp:<target_id>`; остаток и стоп после события сохраняются
- [x] 1.3 Добавить legacy-уведомления `_emit("fill", now, trade_id, …)` для стопа
      и цели («Защитный стоп … исполнен» / «Цель … исполнена»), чтобы факт закрытия
      виделся в консоли/Telegram
- [x] 1.4 Ключ бара — детерминированная строка времени (ISO без tz), один защитный
      акт на бар на сделку

## 2. Редьюсер: приём broker-initiated филлов (D2)

- [x] 2.1 В `ExecutionReducer.apply` (`src/trade_journal/reducer.py`), если
      `event.command_id` содержит маркер `:pv:`: `INSERT OR IGNORE` пары
      `outbox`(status=`SENT`) + `orders`(order_id=command_id, action_type=`STOP`
      либо `TARGET:<target_id>`, quantity=filled_quantity, requested_price=NULL),
      затем применять существующий путь филла без изменений
- [x] 2.2 Проверить фильтр `claim_outbox` (не подхватывает `SENT`) и добавить
      unit-тест: синтетические заявки не выдиспатчиваются
- [x] 2.3 Тест идемпотентности: повторная доставка события одного бара не создаёт
      второго филла и не задваивает объёмы

## 3. restore(): защита переживает перезапуск (D3)

- [x] 3.1 `storage.load_trades` (или хелпер) добавляет в `RecoveredTrade` поле
      `target_filled` из `targets.filled_quantity`
- [x] 3.2 `manager.restore()` перед `register_trade` проверяет
      `broker.has_trade(trade_id)`; для новой сделки `broker.resume_trade(recovered)`
      восстанавливает позицию (qty/avg/side/stop_price в PositionManager),
      `entry_quantity`, `confirmed_stop` (= `state.confirmed_stop` или
      `plan.stop_price`, pending_stop не применяется), `target_filled`,
      `revision = state_revision`, `opened_bar=None`
- [x] 3.3 Живое состояние сделки того же процесса не перезаписывается
      (регистрация остаётся `setdefault`)
- [x] 3.4 Тест «после перезапуска»: открытая сделка со стопом и частично взятой
      целью продолжает защищаться следующим баром; стоп доложается событием,
      целевой остаток не задваивается

## 4. Проверки

- [x] 4.1 Прогнать тесты (`pytest -q`) и линт (`ruff check src tests`)
- [x] 4.2 Добавить/обновить CHANGELOG (Unreleased) и прогнать
      `openspec validate --changes emit-protective-close-events`