## Context

- `src/market_structure/` — пустой каркас дома рыночной структуры (создан в change `market-layering`, архивирован). Это нейтральный дом: запрещены любые импорты из `src/strategies` (включая `contracts`), иначе лакмусовое правило слоёв нарушено. Примитивы рассчитываются по рынку, а не по стратегиям.
- `src/strategies/` — плоские файлы стратегий + общие `indicators/` (подпапки) и `signals.py`. Контракт: файл-стратегия `src/strategies/<имя>.py`, регистрация в реестре через `@register` (авто-dicovery, скип-лист `{"registry","contracts","strategy","signals","names","base"}`), `DEFAULT_CONFIG` = `StrategyConfig(name, strategy_window, indicators)`, `compute(df)` → DataFrame с сигнальными колонками, `decide(ta, timeframe)` → `Decision`. Движок (`src/bot/trading_bot.py`) строит `strategy_map` через `get_strategy(name, config)` по словарям `SHARE_STRATEGIES`/`FUTURE_STRATEGIES` из `src/config.py`.
- Практика snapshot (фактическая, отличается от описания стратегий в документах): `helper.expected_events(strategy_name, ta, config)` — функции определения эталона живут в `tests/snapshot/helper.py`, а не в файлах стратегий. Раннер `tests/snapshot/test_strategies.py` тестирует кейс+стратегию только если существует файл `*_expected_signals.csv` (авто-dicovery по `DATA_DIR`), поэтому для новой стратегии достаточно добавить эталон на существующих свечах `BR_1h`/`NG_1h`.
- `tools/download_snapshot_data.py` ссылается на `strategy.expected_events()`, которого нет у реальных стратегий (устарел относительно рефакторинга); для harmonic использовать штатный путь раннера `--update-snapshots`, а не этот скрипт.

## Goals / Non-Goals

**Goals**
- Наполнить `src/market_structure/` тремя примитивами: `swings.py` (развороты), `fibonacci.py` (уровни и зона попадания), `harmonic.py` (валидация формации 0.2, зеркало лонг/шорт, проекция целей D).
- Добавить стратегию `harmonic_abcd` по контракту (окно = 1), колонку `harmonic_signal`, решение BUY/SELL/HOLD на баре подтверждения формации, не более одного события на формацию, `required_history()`.
- Зарегистрировать `"harmonic_abcd"` в реестре и `StrategyName`, привязать через `SHARE_STRATEGIES["SBER"]`.
- Покрыть unit-тестами детекторы и стратегию; включить стратегию в snapshot на `BR_1h`/`NG_1h`.

**Non-Goals** (отдельные изменения)
- Фибо-SL/TP в `RiskManager` (Уровень 1) — событие BUY/SELL без SL/TP, их заполняют риск и фильтр.
- Стратегия 0.1, кластерный объём (Уровень 3), матрешка таймфреймов (Уровень 4), реальные ордера (Уровень 5).
- Переезд `sr_levels` на общий `swings` — дожидается, пока детектор свингов не «обкатан» здесь.
- Правка `tools/download_snapshot_data.py` и неиспользуемого `strategy.expected_events()` в интерфейсе.

## Decisions

### D1. Примитивы `src/market_structure/` — неизменяемые изолированные расчётчики
- `swings.SwingDetector(left=2, right=2)`: метод `detect(df)` → список неизменяемых записей `SwingPoint(index, price, kind)` (kind: `High`/`Low`); вершина — максимум выше `left` соседей слева и `right` справа, впадина — зеркально по минимумам; пустой при недостаточной глубине.
- `fibonacci`: чистые функции `retracement_level(a, x, ratio)`, `extension_level(a, x, ratio)` (для нисходящей волны X→A: ретрейсмент = `A + ratio*(X−A)`, расширение = `A + 1.618*(X−A)`; для восходящей зеркально), `in_zone(price, level, amplitude, tolerance)` (интервал `[level*(1−tol), level*(1+tol)]`, где `amplitude` — амплитуда контрастного конца волны).
- `harmonic.HarmonicPatternDetector(left=2, right=2, pattern="ab_cd_0.2", fib_tolerance=0.02)`: метод `analyze(df)` → список структур `XabcdPattern` (`{x: SwingPoint, a, b, c, d_target, direction=Long|Short}`) по последним подтверждённым разворотам (X,A,B,C), валидных по геометрии 0.2 (B ∈ [38.2, 61.8]% XA; |AB| ≤ 61.8% |XA|; C ∈ [38.2, 78.6]% AB; D = 161.8% XA). Допуск применяется к амплитуде волны. Поведение специфицировано в `specs/market-structure/spec.md`.
- Никаких импортов из `src.strategies`; только pandas/numpy. Стиль — `TrendAnalyzer`/`SRLevelsCalculator` из `src/market_context/` (класс-расчётчик + dataclass-результат).

### D2. `harmonic_abcd.py`: колонка сигнала кодирует событие и дедупликацию
- `compute(df)` добавляет колонку `harmonic_signal` (BUY=+1, SELL=−1, HOLD=0) на каждой закрытой свече. Событие проставляется только на баре подтверждения формации — это первый бар, на котором сформирован валидный свинг C, геометрия 0.2 валидна и цена находится в отрезке C→D (для лонга D строго выше current close; для шорта зеркально). Дедупликация встроена: формация идентифицируется по индексу бара C, и пока не появилась новая формация с другим C, колонка равна 0 — поэтому `helper.expected_events` для harmonic = `ta[ta["harmonic_signal"] != 0]`, без собственной логики.
- При конфликте (на одном баре возможны бычья и медвежья) приоритет отдаётся более поздней по смещению точки C; таких пар практически не бывает, но правило даёт детерминизм.
- `decide(ta, timeframe)`: читает последний `harmonic_signal` (окно = 1): +1 → `SignalType.BUY`, −1 → `SignalType.SELL`, иначе `SignalType.HOLD`; цена — последняя закрытая; `sl_price`/`tp_price`/`trend`/`decision_string` — `None` (заполняют фильтр и риск).
- `DEFAULT_CONFIG = StrategyConfig(name="harmonic_abcd", strategy_window=1, indicators=())` — сигнальная колонка определяется самой стратегией (не индикаторами), поэтому кортеж индикаторов пустой; `get_last_signals` не используется (как `flat_triangle` в `decide` происходит напрямую по строке).
- `required_history()` переопределён: возвращает прогревочные бары детектора (`detector.warmup`): окно свингов слева/справа (2+2), запас на развитие атомов формации — итого 40. Это НЕ `config.required_history` (тот считает по индикаторам).

### D3. Реестр и конфигурация
- `src/strategies/names.py`: в `StrategyName` добавляется `"harmonic_abcd"` (иначе fail-fast при валидации привязки — разошёлся с реестром).
- `src/config.py`: `"harmonic_abcd"` добавляется в `SHARE_STRATEGIES["SBER"]`. Менять машинерию `strategy_map`/движка не нужно — авто-dicovery цепляет файл, `get_strategy` строит экземпляр по конфигу из словаря.

### D4. Snapshot: кейс SBER_1w (5 лет недельных свечей)
- `tests/snapshot/helper.py`: `STRATEGY_COLUMNS["harmonic_abcd"] = {"event": ["datetime","signal","price"], "float": ["price"]}`; новая функция `_harmonic_abcd_expected_events(ta)` (фильтр по колонке `harmonic_signal != 0`) и ветка в `expected_events()`.
- `tests/snapshot/test_strategies.py`: импорт `DEFAULT_CONFIG` из `src/strategies/harmonic_abcd` и запись в `STRATEGY_CONFIGS`.
- Новый snapshot-кейс `SBER_1w`: акция SBER, ~5 лет недельных свечей (~260 баров), скачивается через `load_candles` в `src.data.loader` (Tinkoff Invest API). За один год недельных свечей (~54 бара) свинг-свингов хватает, но ни одной валидной формации 0.2 нет — глубина выбрана из условия «эталон непустой».
- Эталон `harmonic_abcd_expected_signals.csv` пишется через `helper.expected_events`/`helper.write_expected` из скачанных свечей. Существующие фикстуры `BR_1h`/`NG_1h` не трогаются: они представляют собой гладкие «синусоиды» с полным возвратом (B ≈ 100% XA), на которых формации 0.2 заведомо нет. Раннер привязывается к кейсу: на `SBER_1w` с эталоном гармоники тестируется только `harmonic_abcd`.
- Требование непустоты эталона (task 5.4): на `SBER_1w` должно быть хотя бы одно BUY/SELL — фактически присутствует (лонг на баре 34), иначе пересматривать допуски/константы.

### D5. Тесты
- `tests/unit/market_structure/test_swings.py`: вершина/впадина/недостаток данных/порядок.
- `tests/unit/market_structure/test_fibonacci.py`: уровни ретрейсмента/расширения для нисходящей и восходящей волны, зона допуска (внутри/снаружи/граница).
- `tests/unit/market_structure/test_harmonic.py`: валидные лонг/шорт, C вне допустимого, |AB| превышение, B вне диапазона, допуск, отсутствие формации при нехватке истории.
- `tests/unit/strategies/test_harmonic_abcd.py`: колонка на баре подтверждения, `decide` BUY/SELL/HOLD (включая «цена за D»), дедупликация одной формации, новая формация даёт новое событие, `required_history() >= warmup`, конфликт лонг/шорт детерминирован, `DEFAULT_CONFIG` валиден для `get_strategy`.
- Полный прогон `pytest` (312+ тестов) в конце.

## Risks / Trade-offs

- **Риск: семантика входа (бар подтверждения C, цель D не касается)** — допущение из доков («Покупка от точки C до D, выход в D») сформулировано в спеке; если на практике нужно ждать пробоя C/D или отложенного подтверждения, меняем один блок в `compute()` и эталон.
- **Пустой результат на живых данных** — формация 0.2 жёсткая, событий может быть мало (это фича: редкие высококачественные сигналы), но эталон не должен быть пустым на обоих кейсах; иначе проверить допуски/константы.
- **Требовательность к истории** — 40 баров требуемой истории сверх детекции; на 5-минутках это 3+ часа — приемлемо, на дневках только старший план.
- **Индикаторы не используются** — `indicators=()` может удивить читающего контракт (обычно >0 индикаторов); для стратегии структуры это осознанный компромисс: сигнал строит сама детекция, а не индикаторы.
- **`tools/download_snapshot_data.py` опирается на несуществующий `expected_events()`** — заготовка-холдер для генерации новых кейсов; наша стратегия получает эталон через `--update-snapshots`, и мы сознательно не расширяем этот скрипт в рамках change (Non-Goal).

## Open Questions

- Закрыты в ревью: визуальная отрисовка XABCD на графике — НЕ входит в change (отдельный шаг позже); диагностическая колонка `d_target` в эталоне — НЕ включается, эталон минимальный (datetime/signal/price), направление формации покрывается unit-тестами.