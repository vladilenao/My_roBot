## Why

В торговом режиме робот принимал решение BUY/SELL и показывал его в консоли (`🟢 ПОКУПКА …`), но когда `TradeManager.actions_for_signal` отклонял кандидата (нет метаданных контракта, неизвестный профиль, профиль вернул отказ, нулевой объём после риск-размера, дубликат сигнала, исключение), отказ происходил молча: канал возвращал пустой кортеж, в БД не появлялось ни trades, ни orders, ни processed_signals, и пользователь видел сигнал без объяснения, почему сделка не была допущена.

## What Changes

- `TradeManager.actions_for_signal` вместо молчаливого возврата пустоты при недопуске возвращает структурированный результат допуска (`SignalAdmission`): допущенные действия + список причин недопуска с машиночитаемым кодом и человекочитаемым пояснением на русском.
- Оркестратор доносит в консоль каждую причину недопуска по недопущенному BUY/SELL-кандидату: сообщение вида `⛔ <инструмент> (tf) HH:MM | <стратегия> [<профиль>] ➜ Сделка не допущена: <причина>`.
- Недопуск из-за HOLD/«Нет сигнала» в консоли НЕ выводится (это штатное отсутствие сигнала, а не отказ в допуске).
- Синхронное сопровождение открытых сделок (`manage`) не меняется: причины недопуска добавляются только к входному допуску сигнала.

## Capabilities

### New Capabilities
<!-- Capabilities being introduced. Use kebab-case for path segments you introduce
     (e.g., user-auth or identity/user-auth) that follow the project's existing
     spec organization. Each creates specs/<capability-path>/spec.md. -->

### Modified Capabilities
<!-- Existing capabilities whose REQUIREMENTS are changing (not just implementation).
     Only list here if spec-level behavior changes. Each needs a delta spec file.
     Use the exact existing path under openspec/specs/. Leave empty if no requirement
     changes. A change with no capabilities at all (pure refactor, tooling, docs)
     must set `skip_specs: true` in its .openspec.yaml - openspec validate rejects
     a zero-delta change without that marker. Do not invent a requirement just to
     satisfy validation. -->
- `trade-management`: входной допуск сигнала обязан возвращать результат с причинами недопуска (а не только допущенные действия).
- `notification`: консольное уведомление о недопуске сделки с причиной, без изменения формата обычных сигналов.
- `orchestration`: в торговом режиме по каждому недопущенному BUY/SELL-кандидату доставляется уведомление о недопуске с причинами.

## Impact

- `src/trade_management/manager.py` — `actions_for_signal`: возврат `SignalAdmission`; сбор причин недопуска по веткам отказа.
- `src/notifier/base.py`, `src/execution/port.py` — доставка/формат сообщения о недопуске.
- `src/bot/trading_bot.py` — обработка результата допуска и вывод причин в консоль.
- `tests/unit/trade_management/test_signal_pipeline.py`, `tests/unit/bot/test_trading_bot.py` — обновление под новый возвращаемый тип и тесты причин недопуска.
- Поведение консоли: сигналы по-прежнему показываются как раньше; при недопуске появляется дополнительная строка с причиной.