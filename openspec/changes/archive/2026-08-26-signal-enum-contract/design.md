## Context

После рефакторинга `strategy-ddd-refactor` каждый индикатор имеет свой Enum сигналов в `signals.py`, но базовый класс `Indicator` не содержит контракт на эти Enum-ы. `NO_SIGNAL = 0` продублирован во всех трёх Enum-ах. Потребитель не может программно узнать какие сигналы поддерживает индикатор.

## Goals / Non-Goals

**Goals:**
- Единый базовый класс `BaseSignalEnum` с `NO_SIGNAL = 0`
- Абстрактное свойство `signal_enum` в `Indicator` — контракт для всех подклассов
- Единообразное именование: файлы `signalEnum.py`, классы `*SignalEnum`
- Комментарии к членам Enum-ов и свойствам индикаторов

**Non-Goals:**
- Изменение бизнес-логики сигналов (правила расчёта MACD/RSI/Stoch сохраняются)
- Изменение `SignalType` в `contracts.py` (BUY/SELL/HOLD — другой уровень абстракции)
- Добавление новых индикаторов
- Изменение `StrategyConfig`, `StrategyBuilder`, реестра

## Decisions

### D1: BaseSignalEnum в base.py

**Решение**: `BaseSignalEnum(IntEnum)` с `NO_SIGNAL = 0` в `src/strategies/indicators/base.py`.

**Альтернативы**:
- Дублировать `NO_SIGNAL = 0` в каждом Enum-е — нарушение DRY.
- Константа `NO_SIGNAL = 0` без Enum-а — теряется типизация.

**Rationale**: `BaseSignalEnum` определяет контракт: каждый индикаторный Enum наследует его и автоматически получает `NO_SIGNAL`. Единая точка определения, нет дублирования.

### D2: Абстрактное свойство signal_enum

**Решение**: `Indicator.signal_enum` — абстрактное свойство, возвращающее `type[BaseSignalEnum]`.

**Альтернативы**:
- Классовая переменная `signal_enum: ClassVar[type[BaseSignalEnum]]` — конфликтует с frozen dataclass.
- Метод `get_signal_enum()` — избыточно, property чище.

**Rationale**: Свойство консистентно с `signal_column` и `warmup`. Абстрактное — обязывает каждый подкласс реализовать. Потребитель получает `indicator.signal_enum` для итерации, валидации, документации.

### D3: Файлы signalEnum.py, классы *SignalEnum

**Решение**: Файлы `signalEnum.py` (camelCase), классы `MacdSignalEnum`, `RsiSignalEnum`, `StochasticSignalEnum`.

**Альтернативы**:
- `signal_types.py` / `SignalType` — конфликтует с `SignalType` в `contracts.py`.
- `enums.py` / `*Enum` — менее специфично.

**Rationale**: `signalEnum.py` явно указывает что внутри Enum-ы сигналов. `*SignalEnum` избегает конфликта с `SignalType` (BUY/SELL/HOLD) и чётко описывает назначение.

### D4: Комментарии к Enum-ам и свойствам

**Решение**: Каждый член Enum-а содержит комментарий с описанием условия сигнала. Каждое свойство индикатора содержит docstring.

**Альтернативы**:
- Только docstring к классу Enum — недостаточно для понимания каждого сигнала.
- Без комментариев — нарушение читаемости.

**Rationale**: Комментарии к членам Enum-а позволяют быстро понять условие без перехода к `compute()`. Docstring к свойствам (`signal_column`, `signal_enum`, `warmup`) документируют контракт для потребителей.

## Risks / Trade-offs

- **[Переименование файлов]** → Импорты `from src.strategies.indicators.macd.signals import MacdSignalType` сломаются. **Mitigation**: `__init__.py` реэкспортирует символы; обновить все импорты в одном change.
- **[Переименование классов]** → `MacdSignalType` → `MacdSignalEnum`. **Mitigation**: Поиск всех использований и обновление.
- **[BaseSignalEnum в base.py]** → Увеличивает размер base.py. **Mitigation**: Минимальный overhead (3 строки), изолирован от логики.

## Migration Plan

1. Добавить `BaseSignalEnum(IntEnum)` в `base.py`.
2. Добавить абстрактное свойство `signal_enum` в `Indicator`.
3. Переименовать `signals.py` → `signalEnum.py` в каждой подпапке индикатора.
4. Переименовать классы `*SignalType` → `*SignalEnum`.
5. Сделать все Enum-ы наследниками `BaseSignalEnum`.
6. Добавить комментарии к членам Enum-ов.
7. Реализовать `signal_enum` в каждом индикаторе.
8. Добавить docstring к свойствам индикаторов.
9. Обновить `__init__.py` в подпапках индикаторов.
10. Обновить импорты в тестах и других модулях.
11. Запустить `pytest` и `ruff check` для валидации.

## Open Questions

Нет.
