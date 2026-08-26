## Context

Текущая архитектура стратегии `macd_rsi_stoch`:
- Параметры индикаторов захардкожены в `calculator.py` (MACD 12/26/9, RSI 14, Stoch 14/3/3)
- `STRATEGY_WINDOW = 5` захардкожен в `strategy.py`
- Структура пакета `macd_rsi_stoch/` содержит подпапки `indicators/` и `signals/` — избыточная вложенность
- `tech_analyze()` — единая функция, вычисляющая все три индикатора, не параметризуемая
- `get_last_signals()` жёстко привязана к именам столбцов `macd_signal`, `rsi_signal`, `stoch_signal`

## Goals / Non-Goals

**Goals:**
- Сделать параметры индикаторов конфигурируемыми через Builder Pattern
- Каждый индикатор — self-contained модуль (dataclass + builder + compute)
- `MacdRsiStochStrategy` принимает `StrategyConfig` вместо хардкода
- Упростить структуру: один файл `macd_rsi_stoch.py` вместо подпапок
- Сохранить контракт `Strategy` Protocol без изменений

**Non-Goals:**
- Добавление новых индикаторов (только MACD, RSI, Stochastic)
- Изменение логики генерации сигналов (conserve current signal logic)
- Изменение реестра стратегий (сохраняем `@register` + `get_strategy()`)
- Добавление pydantic как зависимости

## Decisions

### Decision 1: Indicator — frozen dataclass с compute()

**Выбор**: Каждый `Indicator` — frozen dataclass с методом `compute(df)`.

**Альтернативы**:
- Standalone functions в `compute.py` — нарушает Information Expert (данные и поведение разделены)
- Protocol/ABC без dataclass — нет автогенерации `__init__`, `__repr__`, `__eq__`
- Pydantic — лишняя зависимость, проект уже использует dataclasses

**Рationale**: Frozen dataclass + метод `compute()` = Information Expert: класс владеет данными (параметры) и поведением (вычисление). `frozen=True` защищает от случайного изменения параметров во время бэктеста.

### Decision 2: Валидация в __post_init__

**Выбор**: Валидация кросс-зависимостей в `__post_init__` каждого Indicator (fail-fast).

**Альтернативы**:
- Валидация только в `build()` Builder'а — поздно, ошибки накапливаются
- Валидация в `StrategyConfig` — не знает о внутренних зависимостях индикаторов
- Pydantic validators — лишняя зависимость

**Рationale**: `__post_init__` вызывается при создании dataclass (и из Builder'а, и напрямую). Гарантирует, что невалидный индикатор невозможно создать.

### Decision 3: StrategyConfig с tuple индикаторов

**Выбор**: `indicators: tuple[Indicator, ...]` в `StrategyConfig`.

**Альтернативы**:
- `list[Indicator]` — mutable, нарушает иммутабельность
- `frozenset[Indicator]` — нет порядка, важного для воспроизводимости
- `Sequence[Indicator]` — не guarantee immutable

**Рationale**: Tuple immutable и сохраняет порядок. `required_history` и `signal_columns` вычисляются как properties из tuple.

### Decision 4: Структура файлов — плоская

**Выбор**: `src/strategies/<имя>.py` (один файл) + `src/strategies/indicators/` (общий модуль) + `src/strategies/signals.py`.

**Альтернативы**:
- Сохранить `macd_rsi_stoch/` с подпапками — избыточная вложенность для одного класса
- Всё в одном файле — нарушает разделение ответственности (индикаторы vs стратегия)

**Рationale**: Каждая стратегия — один файл. Индикаторы — общий модуль, переиспользуемый стратегиями. `signals.py` — общий модуль агрегации.

### Decision 5: Сохранение tech_analyze для обратной совместимости

**Выбор**: `tech_analyze()` сохраняется, но параметры берутся из переданного конфига (или дефолтов).

**Альтернативы**:
- Удалить `tech_analyze()` — ломает существующие тесты и скрипты
- Оставить без изменений — не поддерживает конфигурируемые параметры

**Рationale**: Минимальный дискомфорт для миграции. Существующие вызовы `tech_analyze(df)` продолжают работать с дефолтами.

## Risks / Trade-offs

- **[Risk] Имена столбцов MACD зависят от параметров** → pandas_ta генерирует столбцы вида `macd_12_26_9`. При изменении параметров имена меняются. **Mitigation**: В `MacdIndicator.compute()` нормализуем имена через `df.columns.str.lower()` и маппим к фиксированным именам `macd`, `macds`, `macdh`.

- **[Risk] Существующие snapshot-тесты могут упасть** → Изменение структуры файлов и импортов. **Mitigation**: Snapshot-тесты используют контракт `Strategy` Protocol, не конкретные классы. Контракт сохраняется.

- **[Risk] Дублирование логики сигналов** → Каждый индикатор содержит свою логику генерации сигналов. **Mitigation**: Логика сигнала специфична для каждого индикатора (RSI — кроссовер 50, MACD — условия ниже/выше нуля, Stoch — кроссовер 20/80). Дублирование оправдано, т.к. логика принципиально разная.

## Migration Plan

1. Создать `src/strategies/indicators/` с `base.py`, `macd.py`, `rsi.py`, `stochastic.py`, `__init__.py`
2. Создать `src/strategies/strategy.py` с `StrategyConfig` + `StrategyBuilder`
3. Создать `src/strategies/signals.py` (перенести `get_last_signals` из `macd_rsi_stoch/signals/aggregate.py`)
4. Создать `src/strategies/macd_rsi_stoch.py` (новая реализация, принимающая `StrategyConfig`)
5. Обновить `src/strategies/__init__.py` (обновить импорты)
6. Удалить `src/strategies/macd_rsi_stoch/` (старая структура)
7. Обновить тесты (импорты)
8. Запустить `pytest` для верификации

**Rollback**: Git revert всех изменений. Старая структура восстанавливается из git history.
