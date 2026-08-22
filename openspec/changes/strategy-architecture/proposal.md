# Архитектура масштабирования стратегий

## Why

Единственная стратегия `macd_rsi_stoch` размазана по слоям: математика вшита в `src/indicators/calculator.py`, агрегация и решения — в `src/signals/generator.py`, цепочка вызовов — в `src/scheduler/runner.py`. Логика «rolling-суммы → консенсус» продублирована трижды (генератор, snapshot-тест, скрипт скачивания). Добавление каждой новой стратегии потребует правок в пяти местах и смешения логик. Стратегия как сущность в коде не существует.

## What Changes

- **BREAKING** Вводятся изолированные пакеты стратегий `src/strategies/<имя>/` (strategy.py + indicators/ + signals/); существующие пакеты `src/indicators/` и `src/signals/` распускаются внутрь пакета `macd_rsi_stoch`.
- **BREAKING** Реестр стратегий (`register`/`get_strategy`) и контракт `Strategy` (NAME, STRATEGY_WINDOW, compute, decide, expected_events, required_history) в `src/strategies/base.py`.
- **BREAKING** Решение — тип `SignalType` (BUY/SELL/HOLD); тексты уведомлений («🚀 ПОКУПАТЬ!») переезжают из стратегии в общий модуль `notifier/formatter.py`. Сила сигнала сознательно не закладывается.
- Глобальный `SIGNAL_WINDOW` удаляется: каждая стратегия владеет своим окном.
- Привязка «многие ко многим» через `STRATEGY_ASSIGNMENTS` в конфиге; инструмент без привязанных стратегий пропускается с записью в лог.
- Движок (`scheduler/runner.py`) становится тонким оркестратором: один запрос свечей на тикер, перебор назначенных стратегий через реестр, изоляция отказа одной стратегии.
- Единый параметризованный snapshot-раннер `tests/snapshot/test_strategies.py` заменяет файл-на-стратегию; добавление стратегии или кейса не требует правки тестов.
- Скрипт скачивания диспетчирует расчёт эталона через реестр (`expected_events` выбранной стратегии), собственная математика из него удаляется. Дефолт глубины остаётся 300; добавлена проверка достаточности истории по `required_history()` стратегии до записи файлов.

## Capabilities

### New Capabilities
- `strategy-contract`: реестр, протокол стратегии, SignalType, разделение решения и форматирования, привязка инструментов к стратегиям, изоляция пакетов и отказов.

### Modified Capabilities
- `signals`: агрегация переезжает внутрь пакета стратегии; решения принимает метод `decide()` (SignalType); тексты сообщений формирует `notifier/formatter`.
- `orchestration`: конвейер перебирает инструменты × назначенные стратегии через реестр; формат уведомления строится formatter'ом; отказ одной стратегии не останавливает остальные.
- `configuration`: `SIGNAL_WINDOW` удалён, добавлен словарь `STRATEGY_ASSIGNMENTS`.
- `snapshot-testing`: единый раннер над всеми кейсами и стратегиями; окно берётся у объекта стратегии; эталон генерируется методом `expected_events` стратегии.
- `test-data`: скрипт скачивания генерирует эталон через реестр и проверяет достаточность истории для окна стратегии.

## Impact

- Перемещения: `src/indicators/calculator.py` → `src/strategies/macd_rsi_stoch/indicators/`; части `src/signals/generator.py` → `src/strategies/macd_rsi_stoch/signals/` (агрегация) и `src/notifier/formatter.py` (тексты).
- Удаления: глобальный `SIGNAL_WINDOW`, `compute_expected_signals` в tools-скрипте, `replay_consensus_events` в тесте, файл `tests/snapshot/test_macd_rsi_stoch.py`.
- Обновление unit-тестов: новые `test_registry.py`, `test_formatter.py`; правки `test_runner.py`, `test_signals.py`.
- Конфигурация: блок контекста `openspec/config.yaml` обновляется под единый раннер.
- ⚠️ Активная смена `instrument-selection-at-startup` содержит устаревшие несинхронизированные дельты на те же capabilities (orchestration, signals, configuration) — её следует заархивировать/выровнять ДО архивации этой смены, иначе при синхронизации устаревшие блоки перезапишут свежие.
