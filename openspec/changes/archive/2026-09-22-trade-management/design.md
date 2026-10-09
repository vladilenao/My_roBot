## Context

См. proposal.md — Why. Продовый рантайм строит только `NotifyOnlyExecutionPort` (`run.py._build_runtime`); `TradeManager` + addressable `JournalBroker` забирают на себя ввод/вывоз. `BrokerExecutionAdapter` (`src/broker/exec_adapter.py`) и обёртка `BrokerExecutionPort` (`src/execution/port.py`) конструируются только в `tests/unit/broker/test_entry_dedup.py` и `tests/unit/execution/test_port.py`; упоминаются в `ARCHITECTURE.md`. `tests/unit/test_run.py` содержит тест, закрепляющий «без legacy-адаптера».

Важное уточнение по границе: `PositionManager` из `src/portfolio/` НЕ удаляется — он живёт внутри `create_addressable_journal_broker` (`src/broker/journal_broker.py`).

## Goals / Non-Goals

**Goals:**
- Убрать мёртвый контур исполнения: `BrokerExecutionAdapter` и `BrokerExecutionPort`.
- Убрать их тестовое покрытие и правки в документации.
- Сохранить без изменений поведение производственного пути и общедоступных API (`ExecutionPort`, `NotifyOnlyExecutionPort`).

**Non-Goals:**
- Удаление `PositionManager`/`ContractMeta`/`Signal` (`src/portfolio/`) — не относятся к удаляемому контуру.
- Переработка дедупликации входов на современном пути (уже реализована другой логикой в допуске/брокере).

## Decisions

1. **Удаление файла адаптера целиком.** `src/broker/exec_adapter.py` удаляется: класс `BrokerExecutionAdapter`, `_guard_entry_once`, `_size_and_place`, `_close_existing`, `_source_label`, импорт `MAX_RISK_PCT`. Единственный потребитель — тест дедупликации, который тоже удаляется. Не ссылался ни один прод-модуль, кроме экспорта `BrokerExecutionPort` из `execution/port.py`.

2. **Удаление `BrokerExecutionPort`.** Класс убирается из `src/execution/port.py`; `src/execution/__init__.py` экспортирует только `ExecutionPort` и `NotifyOnlyExecutionPort`. `ExecutionPort` (абстрактный) и `NotifyOnlyExecutionPort` остаются нетронутыми — это API, которое реально используется (`run.py`).

3. **Сокращение тестового покрытия.** `tests/unit/broker/test_entry_dedup.py` удаляется; `tests/unit/execution/test_port.py` сокращается до тестов `NotifyOnlyExecutionPort`. Функциональность дедупликации на современном пути уже покрыта другими тестами допуска/брокера.

4. **Документация.** Правка таблицы модулей в `ARCHITECTURE.md` (строка про «совместимый адаптер»), без исторических деталей в архиве так далее. Архивные openspec-изменения не редактируются.

## Risks / Trade-offs

- [Скрытый импортер остался вне `src/`/`tests/`] → перед удалением выполняется полный grep по `exec_adapter`/`BrokerExecutionPort`; после — `ruff check` и `pytest` всего дерева.
- [Регрессия современного пути из-за неосторожного удаления общих символов] → `ContractMeta`, `Decision`, `Signal`, `PositionManager` остаются; удаляются только символы, не используемые продом (подтверждено grep и тестом теста run-симуляции).

## Migration Plan

- Единовременное удаление в релизе; откат — восстановление файлов из git. Поведения, требующего миграции данных, нет.

## Open Questions

- Нет.