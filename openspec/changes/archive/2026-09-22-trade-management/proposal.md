## Why

Продовый торговый путь уже давно построен на addressable `JournalBroker` + `TradeManager` (SQLite/outbox), а `run.py` конструирует только `NotifyOnlyExecutionPort`. `BrokerExecutionAdapter` и его обёртка `BrokerExecutionPort` — остатки прежнего контура «решение → сигнал → портфель → ордер», которые в производстве не используются и поддерживаются только юнит-тестами и документацией.

## What Changes

- **BREAKING**: удаляется `src/broker/exec_adapter.py` (`BrokerExecutionAdapter`).
- **BREAKING**: удаляется `BrokerExecutionPort` из `src/execution/port.py` и его экспорт из `src/execution/__init__.py`; остаются `ExecutionPort` (абстрактный) и `NotifyOnlyExecutionPort`.
- Удаляются тесты, покрывавшие удалённый контур: `tests/unit/broker/test_entry_dedup.py` и BrokerExecutionPort-часть `tests/unit/execution/test_port.py`.
- Обновляется справочная документация (`ARCHITECTURE.md`): таблица модулей брокера больше не упоминает совместимый адаптер.
- `PositionManager` в `src/portfolio/` **остаётся** (используется внутри `create_addressable_journal_broker`); `ContractMeta`, `Decision`, `Signal` не затрагиваются.

## Capabilities

### New Capabilities

- нет

### Modified Capabilities

- нет (чистый рефакторинг: удаляется мёртвый код, поведение производственного контура не меняется; `skip_specs: true`)

## Impact

- `src/broker/exec_adapter.py` — удаление файла; `MAX_RISK_PCT` перестаёт потребляться адаптером.
- `src/execution/port.py`, `src/execution/__init__.py` — удаление `BrokerExecutionPort`.
- `tests/unit/broker/test_entry_dedup.py`, `tests/unit/execution/test_port.py` — сокращение до `NotifyOnlyExecutionPort`-покрытия.
- `ARCHITECTURE.md` — правка описания модулей.
- Production-зависимостей нет: `run.py` и бот не ссылаются на удаляемые символы.