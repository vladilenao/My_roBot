## 1. Примитивы `src/market_structure/`

- [x] 1.1 Создать `src/market_structure/swings.py`: `SwingPoint` (dataclass: index/price/kind) и `SwingDetector(left=2, right=2)` с `detect(df)` — вершины/впадины по соседям, пустой список при недостаточной глубине
- [x] 1.2 Создать `src/market_structure/fibonacci.py`: `retracement_level`, `extension_level`, `in_zone` (относительный допуск к амплитуде волны, зеркало для восходящей/нисходящей)
- [x] 1.3 Создать `src/market_structure/harmonic.py`: `XabcdPattern` (x/a/b/c/d_target/direction) и `HarmonicPatternDetector` — валидация геометрии 0.2 (B ∈ [38.2,61.8]% XA, |AB| ≤ 61.8% |XA|, C ∈ [38.2,78.6]% AB, D = 161.8% XA) с допуском; `warmup` (итого 40)
- [x] 1.4 Проверить изоляцию дома: в `src/market_structure/` нет импортов из `src.strategies` (grep-проверка в конце)

## 2. Стратегия `harmonic_abcd`

- [x] 2.1 Создать `src/strategies/harmonic_abcd.py`: `DEFAULT_CONFIG = StrategyConfig(name="harmonic_abcd", strategy_window=1, indicators=())`, `@register`, `compute(df)` → колонка `harmonic_signal` (+1/−1/0), событие только на баре подтверждения C с ценой внутри C→D, дедупликация по индексу C, детерминизм при конфликте лонг/шорт
- [x] 2.2 Реализовать `decide(ta, timeframe)` — BUY/SELL/HOLD по последнему `harmonic_signal`, `sl_price`/`tp_price`/`trend` = None
- [x] 2.3 Реализовать `required_history()` — переопределение с `detector.warmup` (40), не по индикаторам
- [x] 2.4 `src/strategies/names.py`: добавить `"harmonic_abcd"` в `StrategyName`

## 3. Конфигурация и реестр

- [x] 3.1 `src/config.py`: добавить `"harmonic_abcd"` в `SHARE_STRATEGIES["SBER"]`
- [x] 3.2 Проверить `get_strategy("harmonic_abcd", config=DEFAULT_CONFIG)` возвращает стратегию (авто-discovery цепляет файл без правок движка)

## 4. Unit-тесты

- [x] 4.1 `tests/unit/market_structure/test_swings.py`: вершина/впадина/недостаток данных/порядок
- [x] 4.2 `tests/unit/market_structure/test_fibonacci.py`: уровни и зона допуска (внутри/снаружи/граница, зеркало)
- [x] 4.3 `tests/unit/market_structure/test_harmonic.py`: валидные лонг/шорт, B/AB/C-провалы, допуск, нехватка истории
- [x] 4.4 `tests/unit/strategies/test_harmonic_abcd.py`: колонка сигнала на баре подтверждения, decide (BUY/SELL/HOLD, «цена за D»), дедупликация одной формации + новая формация → новое событие, `required_history()`, детерминизм конфликта

## 5. Snapshot

- [x] 5.1 `tests/snapshot/helper.py`: `STRATEGY_COLUMNS["harmonic_abcd"]`, функция `_harmonic_abcd_expected_events` (фильтр `harmonic_signal != 0`) и ветка в `expected_events()`
- [x] 5.2 `tests/snapshot/test_strategies.py`: импорт `DEFAULT_CONFIG` и запись в `STRATEGY_CONFIGS`
- [x] 5.3 Скачать фикстуру `tests/snapshot/data/SBER_1w/candles.csv` (акция SBER, ~5 лет недельных свечей через `load_candles`) и сгенерировать через `helper.expected_events` эталон `harmonic_abcd_expected_signals.csv`
- [x] 5.4 Убедиться, что эталон не пустой (есть хотя бы одно BUY/SELL на кейс SBER_1w), иначе пересмотреть допуски/константы

## 6. Финальная проверка

- [x] 6.1 Полный `pytest` — все тесты зелёные (312 существующих + новые, эталоны прочих стратегий не тронуты)
- [x] 6.2 `rg "src\.strategies" src/market_structure` — ноль совпадений
- [x] 6.3 `openspec validate` — change валиден