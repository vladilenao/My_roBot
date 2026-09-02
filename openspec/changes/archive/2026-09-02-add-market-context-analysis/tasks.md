## 1. Модели и расширение Decision

- [x] 1.1 Создать `src/analysis/models.py`: `TrendDirection`, `TrendResult`, `SRType`, `SRLevel`, `MarketContext` (все frozen dataclass / enum) согласно spec `market-analysis`.
- [x] 1.2 Расширить `Decision` в `src/strategies/contracts.py`: добавить поля `stop_loss`, `take_profit`, `sl_distance_pct`, `tp_distance_pct`, `sl_level_label`, `tp_level_label`, `trend_direction`, `trend_confidence` с дефолтом None (обратная совместимость).

## 2. Анализатор тренда

- [x] 2.1 Создать `src/analysis/trend.py`: `TrendAnalyzer` — frozen dataclass (ema_short=20, ema_long=50, adx_period=14), метод `analyze(df) → TrendResult` на основе EMA-кроссовера и ADX (через pandas_ta_classic).
- [x] 2.2 Покрыть unit-тестами (`tests/unit/`): восходящий/нисходящий/боковой тренд, недостаточная глубина истории.

## 3. Калькулятор уровней S/R

- [x] 3.1 Создать `src/analysis/sr_levels.py`: `SRLevelsCalculator` — frozen dataclass (fractal_bars=5, max_levels=6, min_touches=2), метод `compute(df, current_price) → list[SRLevel]` на основе фракталов и кластеризации.
- [x] 3.2 Покрыть unit-тестами: определение уровней, min_touches, max_levels (возврат ближайших к цене).

## 4. MarketContextCache

- [x] 4.1 Создать `src/analysis/context_cache.py`: `MarketContextCache(data_cache, trend_analyzer, sr_calculator)` — lazy-кэш по ключу (ticker, type), инвалидация сравнением `datetime` последней свечи в tz-naive виде.
- [x] 4.2 Покрыть unit-тестами: первый запрос, повторный запрос без пересчёта, новая свеча → пересчёт, пустые данные → пустой результат.

## 5. SignalFilter

- [x] 5.1 Создать `src/analysis/filter.py`: `SignalFilter` — frozen dataclass, метод `apply(decision, ctx) → Decision` (жёсткий фильтр: down+BUY/up+SELL → HOLD; обогащение trend_direction/trend_confidence; confidence=0.0 при блоке).
- [x] 5.2 Покрыть unit-тестами: блок против тренда, пропуск по тренду, боковой тренд, HOLD без изменений, обогащение полей.

## 6. RiskManager

- [x] 6.1 Создать `src/analysis/risk.py`: `RiskManager` — frozen dataclass (risk_reward_ratio=2.0, default_sl_pct=0.02), метод `apply(decision, ctx) → Decision` (SL по ближайшему уровню, TP по risk/reward с коррекцией на уровень S/R, fallback 2%).
- [x] 6.2 Покрыть unit-тестами: BUY с support/resistance, SELL с resistance, нет уровней (fallback), HOLD без изменений, заполнение всех полей.

## 7. Интеграция в оркестратор

- [x] 7.1 Создать `src/analysis/__init__.py` с экспортами.
- [x] 7.2 Изменить `TradingBot._tick()`: пропускать пустой тик (`has_fresh_closed_bar()` → False ⇒ `return` без `_process`, без heartbeat) — согласно spec `orchestration`.
- [x] 7.3 Изменить `TradingBot._analyze()`: получать `MarketContext` один раз на инструмент (из MarketContextCache), прогонять Decision через SignalFilter и RiskManager перед `_emit`.
- [x] 7.4 Обновить `run.py`: собрать `TrendAnalyzer`, `SRLevelsCalculator`, `MarketContextCache`, `SignalFilter`, `RiskManager`, `MarketDataCache` и передать в `TradingBot`.

## 8. Тесты интеграции и проверка

- [x] 8.1 Убедиться, что существующие snapshot-тесты стратегий (`tests/snapshot/test_strategies.py`) не требуют изменений (compute/decide не изменены).
- [x] 8.2 Покрыть unit-тестами пропуск пустого тика в оркестраторе (mock data_cache/timeline).
- [x] 8.3 Прогнать весь набор тестов (`pytest`) и убедиться в отсутствии регрессий.
- [x] 8.4 Запустить `ruff`/линтер по новым модулям `src/analysis/`.

## 9. Snapshot-тесты analysis-слоя

- [x] 9.1 Создать `tests/snapshot/test_analysis.py`: отдельный раннер, параметризующийся по папкам кейсов, сканирующий файлы `*_expected_context.csv` (не пересекается с `test_strategies.py` и `*_expected_signals.csv`).
- [x] 9.2 Реализовать в раннере диспетчеризацию по типу эталона: `trend` → `TrendAnalyzer.analyze(df)`, `sr_levels` → `SRLevelsCalculator.compute(df, price)`; вычислить фактические `trend`/`sr_levels` и сформировать DataFrame-результат.
- [x] 9.3 Дополнить `tests/snapshot/helper.py` (или новый helper) функциями загрузки/сравнения/записи analysis-эталонов: `load_analysis_expected`, `compare_analysis`, `write_analysis_expected`, `first_analysis_divergence` (через `assert_frame_equal`, rtol, указание первой расходящейся строки).
- [x] 9.4 Сгенерировать эталоны для существующих кейсов: `trend_expected_context.csv` и `sr_levels_expected_context.csv` для `BR_1h` и `NG_1h` (прогон с `--update-snapshots`, diff ревьюируется).
- [x] 9.5 Проверить поддержку `--update-snapshots` для `test_analysis.py` (опция из `tests/conftest.py`) — осознанное обновление analysis-эталонов и обычный прогон без изменений файлов.
- [x] 9.6 Прогнать `pytest tests/snapshot/test_analysis.py` и убедиться, что проверка детерминированности тренда и уровней S/R работает на фиксированных свечах.

