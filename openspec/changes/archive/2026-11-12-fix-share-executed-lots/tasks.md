## 1. Исполнение в лотах нашей заявки

- [x] 1.1 `executed_lots(card, lots_executed, lots_requested)` в `src/broker_contour/prices.py`: больше лотов заявки, кратно лоту и после деления не больше лотов заявки — штуки, делится на лот; иначе как есть.
- [x] 1.2 `TInvestBroker._in_lots`: отчёт брокера переводится в лоты заявки журнала в начале `_apply_report` и в `_refresh` до проведения заглушки.
- [x] 1.3 `_refresh`: заглушка `tinvest-order:` только при остатке `min(lots_executed, лоты заявки) − исполнено` больше нуля.
- [x] 1.4 `_apply_report`: в досверку — только если `min(lots_executed, лоты заявки)` больше исполненного.

## 2. Тесты

- [x] 2.1 `tests/unit/broker_contour/test_tinvest_broker.py`:
  - `test_executed_lots_from_shares` — штуки, лоты, не кратно лоту, лот 1;
  - `test_order_state_in_shares_after_stream_trade_does_not_divide_by_zero` — случай песочницы 11.11 (GAZP: сделка стрима 10 лотов, `GetOrderState` `lots_executed=100`); до правки — `DivisionByZero`;
  - `test_order_state_in_shares_without_trades_books_lots` — заглушка 10 лотов по 116,82; до правки — `DivisionByZero`;
  - `test_order_state_over_order_lots_is_ignored_without_resync` — лишние лоты не в штуках: исполнения нет, `GetOrderState` не повторяется; до правки — досверка на каждом шаге.
- [x] 2.2 Полный `pytest` и `ruff check src tests run.py`.
  Сделано 11.11.2026: 4095 passed, `ruff check` чистый.

## 3. Живая проверка на тестовом роботе

- [x] 3.1 Контур «Песочница» перезапущен на правке 11.11 в 20:24: ошибок нет, стоп GAZP у брокера выставлен (SELL 10 лотов, 113,46), стоп YDEX передвинут (3403).
- [x] 3.2 Следующий вход в акцию с лотом больше 1 исполнен в песочнице: в логе контура нет `DivisionByZero`, в журнале заявок исполнено столько лотов, сколько в заявке, стоп у брокера выставлен.
  Засчитано владельцем 12.11.2026 по выходу (тот же путь отчёта): GAZP SELL лимитом 1 лот в 14:00:16 МСК, `GetOrderState` — `FILL`, запрошено 1, исполнено 10 (штуки), `stages` пусты; в журнале заявок 1 лот по 116,26, стоп у брокера перевыставлен на 9 лотов по 114,28. `DivisionByZero` после рестарта на правке (11.11 20:24) нет.
