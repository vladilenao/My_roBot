## Why

В `strat.docx` описана торговля по гармоническим фибо-формациям AB=CD (стратегия 0.2): вход от точки C после подтверждения формации XABCD, выход в точке D на 161.8% волны XA. Робот сейчас знает только индикаторные стратегии (MACD+RSI+Stochastic, BB-флэт). Нужна новая стратегия `harmonic_abcd`, использующая зарезервированное в `market-layering` место `src/market_structure/` для чистых примитивов структуры рынка (свинги, фибо, гармоники) — с зеркальной поддержкой лонга и шорта по скрину «Формация в лонг/шорт».

## What Changes

- **Новые примитивы в `src/market_structure/`** (нейтральный дом, без зависимостей от `strategies`):
  - `swings.py` — детекция свинговых вершин/впадин (фракталы, `left/right` бары);
  - `fibonacci.py` — расчёт уровней ретрейсмента/расширения волны (38.2/50/61.8/78.6/161.8%) и проверка «цена внутри уровня с допуском»;
  - `harmonic.py` — детектор формации XABCD 0.2: валидация геометрии B/C и проекция цели D.
- **Новая стратегия `src/strategies/harmonic_abcd.py`** — собственный файл по `strategy-contract`: `@register`, `DEFAULT_CONFIG`, `compute()` (колонка сигнала `harmonic_signal`), `decide()`, `expected_events()`, `required_history()`; окно = 1.
- **Сигнальная семантика**: BUY на закрытой свече, где подтверждена бычья формация 0.2 (вход в C→D-отрезке, цель D выше цены); SELL — зеркально для медвежьей; HOLD в остальных случаях. D — цель, а не точка касания (вход от C, выход в D).
- `src/strategies/names.py` — в `StrategyName` добавляется `"harmonic_abcd"` (консистентность с реестром).
- `src/config.py` — `harmonic_abcd` добавляется в `SHARE_STRATEGIES["SBER"]` (активация через конфиг, как требует контракт).
- **Тесты**: unit на `market_structure` (`tests/unit/market_structure/`) и на стратегию (`tests/unit/strategies/test_harmonic_abcd.py`); snapshot — `STRATEGY_COLUMNS` и ветка `expected_events()` в `tests/snapshot/helper.py`, `STRATEGY_CONFIGS` в `tests/snapshot/test_strategies.py`, новый кейс `SBER_1w` (акция SBER, ~5 лет недельных свечей, скачанных через `load_candles`) с эталоном `harmonic_abcd_expected_signals.csv`.
- Внешних поведенческих изменений других стратегий нет.

## Capabilities

### New Capabilities
- `market-structure`: поведение примитивов `src/market_structure/` — детекция свингов, расчёт фибо-уровней и проверка валидности формации XABCD 0.2 (лонг и шорт, зеркально) с допусками.
- `harmonic-abcd-strategy`: поведение стратегии `harmonic_abcd` — контракт на колонку сигнала, событие BUY/SELL при подтверждении формации 0.2, отсутствие дублей, требуемая история, консистентность имени с реестром.

### Modified Capabilities
- Нет: `strategy-contract`, `strategy-registry`, `signals`, `test-layout`/`snapshot-testing` уже описывают требуемое поведение (файл-стратегия, авто-discovery, консистентный `names.py`, автоматический подхват snapshot-раннером); добавление новой стратегии не меняет их требований.

## Impact

- Создаётся: `src/market_structure/swings.py`, `src/market_structure/fibonacci.py`, `src/market_structure/harmonic.py`, `src/strategies/harmonic_abcd.py`.
- Правки: `src/strategies/names.py`, `src/config.py`, `tests/snapshot/helper.py`, `tests/snapshot/test_strategies.py`; новая фикстура `tests/snapshot/data/SBER_1w/candles.csv` + эталон `tests/snapshot/data/SBER_1w/harmonic_abcd_expected_signals.csv`.
- Новые тесты: `tests/unit/market_structure/`, `tests/unit/strategies/test_harmonic_abcd.py`.
- Не входит сюда (отдельные шаги): фибо-SL/TP в RiskManager (Уровень 1), кластерный объём, мультитаймфрейм, переезд `sr_levels` на общий `swings`, параметры стратегии 0.1.