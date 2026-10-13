## 1. Консольные шаблоны заявки

- [x] 1.1 В `src/notifier/templates/deal.py` в `_order_accepted` убрать хвост `(id={order_id})`; сообщение остаётся `📝 Ордер: Заявка <сторона> <объём> <инструмент> по <цена> принята`
- [x] 1.2 В `src/notifier/templates/deal.py` в `_trade_cancelled` заменить `event.get('order_id')` на короткое имя инструмента (`event.instrument`): ветка TTL — `Заявка <инструмент> истекла по TTL`, иначе — `Заявка <инструмент> отменена (<причина>)`

## 2. События отмены от брокера

- [x] 2.1 В `src/broker/journal_broker.py` в `cancel_order` добавить `instrument=self._display(order.ticker)` в публикуемое событие `trade_cancelled`
- [x] 2.2 В `src/broker/journal_broker.py` в `_expire_order` добавить `instrument=self._display(order.ticker)` в публикуемое событие `trade_cancelled`
- [x] 2.3 Проверить, что путь отмены по таймауту входа (`run.py:publish_execution` → `execution_observer`) уже передаёт `instrument`; при необходимости выровнять с остальными издателями

## 3. Тесты

- [x] 3.1 Обновить `tests/unit/notifier/test_templates.py`: убрать `(id=42)` из ожидания `order_accepted` и заменить `Заявка 42 …` на короткое имя инструмента для трёх сценариев `trade_cancelled` (`risk_cap`, `entry-timeout`, `ttl`)
- [x] 3.2 Добавить unit-тест, что `trade_cancelled` от прямого издателя брокера (`cancel_order`/`_expire_order`) несёт короткое имя инструмента и консольный текст не содержит `None` и сырого тикера
- [x] 3.3 Убедиться, что тесты не ожидают числовой `order_id` в пользовательском тексте где-либо ещё (`grep` по `id=`/`Заявка 42`)

## 4. Документация

- [x] 4.1 Обновить примеры консольных строк в `docs/notification/events.md` (`order_accepted`, `trade_cancelled`, истечение TTL), убрав `id=…` и показав короткое имя инструмента

## 5. Внутренние сообщения результата заявки

- [x] 5.1 В `src/broker/journal_broker.py` убрать `(id={order_id})` из внутренних `OrderResult.message` (`place_order`) для единообразия с решением «убирать везде»; убедиться, что поле нигде не выводится пользователю

## 6. Проверка

- [x] 6.1 Запустить `python3 -m pytest tests/unit/notifier tests/unit/broker -q`
- [x] 6.2 Запустить полный `python3 -m pytest -q` и убедиться, что все тесты зелёные
