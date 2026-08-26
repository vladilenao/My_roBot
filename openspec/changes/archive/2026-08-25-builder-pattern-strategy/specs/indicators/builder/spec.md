## Purpose

Предоставляет компонентный Builder Pattern для индикаторов технического анализа: каждый индикатор (MACD, RSI, Stochastic) инкапсулирует свои параметры в frozen dataclass и предоставляет Builder с method chaining для конфигурации.

## ADDED Requirements

### Requirement: Каждый индикатор — frozen dataclass
Каждый тип индикатора ДОЛЖЕН быть реализован как `dataclass(frozen=True)` с параметрами по умолчанию, соответствующими текущему хардкоду: MACD(fast=12, slow=26, signal=9), RSI(period=14), Stochastic(k=14, d=3, smooth_k=3).

#### Scenario: Индикатор неизменяем после создания
- **WHEN** создан экземпляр `MacdIndicator`
- **THEN** попытка изменения атрибута вызывает `FrozenInstanceError`

#### Scenario: Дефолты соответствуют текущему хардкоду
- **WHEN** создан `MacdIndicator()` без параметров
- **THEN** `fast=12`, `slow=26`, `signal=9`

### Requirement: Builder с method chaining
Каждый тип индикатора ДОЛЖЕН иметь соответствующий Builder-класс с методами `set_<param>()`, возвращающими `self`, и финальным методом `build()`, возвращающим frozen dataclass.

#### Scenario: Цепочка вызовов
- **WHEN** вызывается `MacdIndicatorBuilder().set_fast(8).set_slow(17).set_signal(5).build()`
- **THEN** возвращается `MacdIndicator(fast=8, slow=17, signal=5)`

#### Scenario: Build без вызовов setter
- **WHEN** вызывается `MacdIndicatorBuilder().build()`
- **THEN** возвращается `MacdIndicator` с дефолтными значениями

### Requirement: Валидация параметров индикатора
Каждый индикатор ДОЛЖЕН валидировать свои параметры в `__post_init__`: MACD — `fast < slow` и `signal < slow`; Stochastic — `k >= d`; все параметры > 0.

#### Scenario: MACD fast >= slow
- **WHEN** создаётся `MacdIndicator(fast=26, slow=12)`
- **THEN** выбрасывается `ValueError` с описанием нарушения

#### Scenario: Stochastic k < d
- **WHEN** создаётся `StochasticIndicator(k=3, d=14)`
- **THEN** выбрасывается `ValueError` с описанием нарушения

### Requirement: Warmup property
Каждый индикатор ДОЛЖЕН предоставлять property `warmup`, возвращающий количество баров для прогрева: MACD — `slow + signal`, RSI — `period`, Stochastic — `k + smooth_k`.

#### Scenario: Warmup MACD
- **WHEN** создан `MacdIndicator(fast=12, slow=26, signal=9)`
- **THEN** `warmup == 35`

#### Scenario: Warmup RSI
- **WHEN** создан `RsiIndicator(period=14)`
- **THEN** `warmup == 14`

### Requirement: Signal column property
Каждый индикатор ДОЛЖЕН предоставлять property `signal_column`, возвращающий имя столбца с сигналом: `"macd_signal"`, `"rsi_signal"`, `"stoch_signal"`.

#### Scenario: Signal column MACD
- **WHEN** создан `MacdIndicator()`
- **THEN** `signal_column == "macd_signal"`

### Requirement: Compute method
Каждый индикатор ДОЛЖЕН предоставлять метод `compute(df)`, который вычисляет индикатор и генерирует сигнальный столбец на копии DataFrame. Исходный DataFrame НЕ ДОЛЖЕН быть изменён.

#### Scenario: Compute добавляет сигнальный столбец
- **WHEN** вызывается `MacdIndicator().compute(ohlcv_df)`
- **THEN** возвращается DataFrame с добавленным столбцом `macd_signal`

#### Scenario: Исходный DataFrame не изменяется
- **WHEN** вызывается `MacdIndicator().compute(ohlcv_df)`
- **THEN** исходный `ohlcv_df` сохраняет оригинальные столбцы
