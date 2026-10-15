## 1. Воспроизведение

- [x] 1.1 В `tests/unit/trade_management/test_entry_enqueue_time.py` воспроизвести дефект на настоящих `Storage`, `TradeManager` и `run._dispatch_evidence`: план со временем свечи 04:45, часы менеджера 05:00:02, `intent_since` = 05:00:01, данные подтверждены. Тест фиксирует поведение спецификации (вход доставлен в своём шаге, `outbox` `SENT`) и падает на текущем коде (`freshness-unconfirmed`, вход снят следующим `cancel_undelivered_increases`).

## 2. Исправление

- [x] 2.1 В `TradeManager` запоминать момент постановки в `_insert_action` (`command_id → as_aware(clock.now())`) и добавить `enqueued_at(command)` с возвратом `command.created_at` для неизвестной команды.
- [x] 2.2 В `_dispatch_eligible` передавать инспектору данных команду с моментом постановки; `run._dispatch_evidence` и `created_before_step` не менять.
- [x] 2.3 В `cancel_undelivered_increases` отбирать кандидатов по `enqueued_at`. Освобождать запись после доставки и снятия.
- [x] 2.4 Не менять `outbox.created_at`, `claim_outbox` и `execution_bar_boundaries`.

## 3. Тесты

- [x] 3.1 Покрыть сценарии дельт на настоящем reducer:
  - вход со свечой 04:45 и подтверждёнными данными доставляется в шаге создания и не снимается на следующем шаге;
  - тот же вход без подтверждённых данных снимается на следующем шаге с причиной `entry-undelivered`;
  - после «перезапуска» (новый `TradeManager` на той же базе) зависший вход со временем свечи снимается на первом шаге;
  - `outbox.created_at` входа остаётся временем свечи, а `execution_bar_boundaries` не меняется;
  - добор и закрывающие команды (`submit_action`) ведут себя как раньше.
- [x] 3.2 Прогнать `tests/unit/trade_management/test_undelivered_increases.py`, `test_dispatch_eligibility.py`, `tests/unit/test_live_causal_processing.py` без правок ожиданий.

## 4. Завершение

- [x] 4.1 В `docs/trade-management/portfolio-risk.md` рядом с описанием снятия добавить абзац: момент создания команды — момент постановки, а не время свечи.
- [x] 4.2 Прогнать `pytest tests/` и `openspec validate fix-entry-command-enqueue-time --strict`. Коммиты делать только с зелёными тестами, сообщения на русском.
