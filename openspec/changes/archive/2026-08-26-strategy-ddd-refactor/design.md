## Context

Текущая реализация стратегии `macd_rsi_stoch` использует плоскую структуру `indicators/`, дефолтные значения параметров в dataclass-ах, целочисленные сигналы (+1/-1/0) и fallback `config=None` в конструкторе стратегии. Реестр `get_strategy(name)` не передаёт конфиг. Builder-ы индикаторов избыточны. См. proposal.md — Why.

## Goals / Non-Goals

**Goals:**
- DDD: Strategy = Aggregate Root, Indicators = Value Objects внутри агрегата
- Явная передача параметров — запрет на дефолты
- Семантические Enum-ы вместо целых чисел для сигналов
- Иерархическая структура `indicators/<name>/`
- Конфигурация в начале файла стратегии

**Non-Goals:**
- Изменение бизнес-логики сигналов (правила расчёта MACD/RSI/Stoch сохраняются)
- Изменение `StrategyConfig`, `StrategyBuilder`, `contracts.py`, `signals.py`
- Добавление новых стратегий
- Изменение конфигурации `src/config.py` (привязки инструментов)

## Decisions

### D1: IntEnum для сигналов вместо普通的 Enum

**Решение**: `MacdSignalType(IntEnum)`, `RsiSignalType(IntEnum)`, `StochasticSignalType(IntEnum)`.

**Альтернативы**:
- Обычный `Enum` — потребует конвертации в `int` перед суммированием в `decide()`. Дополнительная сложность без benefit.
- Отдельные `@dataclass` для каждого сигнала — избыточно, теряется арифметика.

**Rationale**: `IntEnum` даёт и именованные константы (читаемость), и арифметику (суммирование в `decide()` работает напрямую). Существующий код `get_last_signals()` и `decide()` продолжит работать без изменений.

### D2: Подпапки вместо плоских файлов

**Решение**: `indicators/macd/{__init__,indicator,signals}.py`, аналогично `rsi/` и `stochastic/`.

**Альтернативы**:
- Внутренние классы стратегии — раздули бы `macd_rsi_stoch.py` до 300+ строк, нарушение SRP.
- Плоские файлы + Enum в отдельном модуле — хаотичная структура, Enum远离 от логики индикатора.

**Rationale**: Подпапки дают namespace isolation (`indicators.macd.indicator.MacdIndicator`), изолируют Enum-ы от логики других индикаторов, следуют принципу "каждый индикатор — отдельный модуль". DDD aggregate boundary соблюдается через паттерн использования: индикаторы инстанциируются только через `StrategyConfig`.

### D3: Обязательный config в конструкторе стратегии

**Решение**: `MacdRsiStochStrategy(config: StrategyConfig)` — без `Optional`, без fallback.

**Альтернативы**:
- `config=None` с fallback на `DEFAULT_CONFIG` — сохраняет обратную совместимость, но противоречит требованию "явная передача".
- Дефолты в dataclass-ах индикаторов — пользователь может не заметить молчаливые значения.

**Rationale**: Явный config = компилятор/линтер поймает забытый аргумент. Стратегия не может существовать без конфигурации — это агрегатный корень, которому required part = indicators.

### D4: get_strategy(name, config) — config обязателен

**Решение**: Сигнатура `get_strategy(name: str, config: StrategyConfig) -> Strategy`.

**Альтернативы**:
- `config: StrategyConfig | None = None` — fallback на дефолты, противоречит D3.
- Два метода: `get_strategy(name)` для дефолтов, `get_strategy_with_config(name, config)` — избыточно, путает API.

**Rationale**: Единый метод с обязательным config. Вызывающий код (runner, download_snapshot_data, тесты) явно создаёт и передаёт config. Это единообразно и предсказуемо.

### D5: Удаление индикаторных Builder-ов

**Решение**: Удалить `MacdIndicatorBuilder`, `RsiIndicatorBuilder`, `StochasticIndicatorBuilder`. Оставить `StrategyBuilder`.

**Альтернативы**:
- Оставить Builder-ы как есть — избыточный код, дублирует дефолты, противоречит "no defaults".
- Оставить, но убрать дефолты из Builder-ов — Builder без дефолтов не несёт ценности.

**Rationale**: `MacdIndicator(fast=12, slow=26, signal=9)` столь же читаемо, как builder chain. Builder оправдан только при сложном конструировании (5+ параметров, валидация между ними). У индикаторов 2-3 параметра — builder избыточен. `StrategyBuilder` остаётся: он валидирует бизнес-правила (имя обязательно, window > 0, хотя бы один индикатор).

### D6: Конфигурация в начале файла стратегии

**Решение**: Модульная константа `DEFAULT_CONFIG = StrategyConfig(...)` в начале `macd_rsi_stoch.py`.

**Альтернативы**:
- Словарь параметров — теряет типизацию и валидацию.
- Конфиг в `config.py` — нарушает принцип "стратегия владеет своей конфигурацией".

**Rationale**: `StrategyConfig` — frozen dataclass, типизирован и неизменяем. Размещение в начале файла стратегии даёт мгновенную видимость параметров. `DEFAULT_CONFIG` переиспользуется runner-ом, тестами, download_snapshot_data.

## Risks / Trade-offs

- **[Сломать обратную совместимость]** → Все вызовы `get_strategy(name)` без config сломаются. **Mitigation**: Поиск всех вызовов (runner.py, download_snapshot_data.py, тесты) и обновление в одном change.
- **[Удаление Builder-ов]** → Если появится стратегия с 5+ параметрами индикаторов, builder может понадобиться. **Mitigation**: Builder легко добавить обратно; пока benefit не оправдан.
- **[Enum-ы возвращаются из compute()]]** → `get_last_signals()` суммирует значения. **Mitigation**: `IntEnum` совместим с `int`, суммирование работает напрямую.
- **[Переименование файлов]** → Импорты в тестах и других модулях сломаются. **Mitigation**: `__init__.py` в подпапках реэкспортирует символы, старые импорты через `from src.strategies.indicators.macd import MacdIndicator` продолжат работать.

## Migration Plan

1. Создать подпапки `indicators/macd/`, `indicators/rsi/`, `indicators/stochastic/` с `__init__.py`, `indicator.py`, `signals.py`.
2. Перенести логику индикаторов из плоских файлов в `indicator.py`, добавить Enum-ы в `signals.py`.
3. Удалить плоские файлы `indicators/macd.py`, `indicators/rsi.py`, `indicators/stochastic.py`.
4. Удалить Builder-ы индикаторов из `indicator.py`.
5. Обновить `base.py` (без изменений, только проверить импорты).
6. Обновить `macd_rsi_stoch.py`: добавить `DEFAULT_CONFIG` в начало, убрать `_build_default_config()`, сделать config обязательным.
7. Обновить `registry.py`: `get_strategy(name, config)`.
8. Обновить `runner.py`: собрать config явно, передать в `get_strategy()`.
9. Обновить `download_snapshot_data.py`: аналогично.
10. Обновить тесты: `test_strategy_builder.py`, `test_registry.py`, `test_signals.py`, `test_strategies.py`.
11. Запустить `pytest` и `ruff check` для валидации.

## Open Questions

Нет.
