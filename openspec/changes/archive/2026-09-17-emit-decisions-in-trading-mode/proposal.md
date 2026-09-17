## Why

В торговом режиме (активный `trade_manager`) решения стратегий перестали доставляться нотификатору: ветка допуска кандидатов в `_analyze` делает `continue`, минуя `_emit`. Сигналы вида `● Si-9.26 (15m) 10:15 | ma_cloud_rsi_macd [triple_screen] ➜ ⏳ Нет сигнала.` исчезают из консоли при включённой торговле — регрессия после рефакторинга журнала сделок. Требование «уведомление на каждом тике по каждой паре инструмент × стратегия» не закреплено в спеке и перезатёрлось при рефакторинге.

## What Changes

- Оркестратор в торговом режиме снова доставляет каждое решение в порт исполнения (`_emit`), не изменяя допуск кандидатов через `trade_manager` и не дублируя допуска.
- Ошибка или HOLD стратегии по-прежнему не отключают сопровождение сделок; нотификация идёт по каждому решению независимо.
- Тест-регрессия `test_decisions_notified_even_in_trading_mode` фиксирует доставку решения в торговом режиме.

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
- `orchestration`: в торговом режиме решение стратегии обязано доставляться нотификатору/порту исполнения на каждом тике, независимо от фазы допуска кандидатов.

## Impact

- `src/bot/trading_bot.py` — `_analyze`: доставка через `_emit` в ветке с `trade_manager`.
- `tests/unit/bot/test_trading_bot.py` — новый тест-регрессия.
- Поведение консольных/Telegram-уведомлений в торговом режиме: решения снова видны пользователю.