## Why

Текущий бот генерирует торговые сигналы без учёта рыночного контекста: каждая стратегия решает независимо, не зная направления тренда и где находятся ключевые уровни поддержки/сопротивления. Это приводит к двум проблемам:
1. Стратегия может рекомендовать BUY во время нисходящего тренда или SELL во время восходящего — против тренда.
2. Стопы и тейки рассчитываются произвольно (или не рассчитываются вообще), а не по реальным рыночным уровням, что ухудшает risk/reward.

Дополнительно: пустые тики (когда свеча не закрылась) запускают полный pipeline вычислений (compute + decide) ~3600 раз в час на 1h таймфрейме впустую.

## What Changes

- **Новый модуль `src/analysis/`**: TrendAnalyzer (EMA + ADX), SRLevelsCalculator (фракталы + кластеризация), MarketContext (frozen dataclass-результат), MarketContextCache (lazy-кэш, инвалидация по свече), SignalFilter (жёсткий фильтр по тренду → HOLD), RiskManager (SL/TP по уровням S/R).
- **Расширение Decision** (`contracts.py`): новые поля `stop_loss`, `take_profit`, `sl_level_label`, `tp_level_label`, `trend_direction`, `trend_confidence` — все с дефолтом `None`, обратно совместимы.
- **Пропуск пустых тиков** (`TradingBot._tick()`): если `has_fresh_closed_bar()` → False, `_process` не вызывается, уведомлений нет, compute/decide не запускаются.
- **Изменение спеки orchestration**: требование «Защита от дублирования сигналов» и сценарии «Уведомление на каждом тике» / «Сигнал без изменений» — заменяются на «Уведомление только при новой закрытой свече».
- **Изменение спеки strategy-contract**: поле Decision расширяется (добавляется требование на SL/TP/trend поля).
- **Точка сборки** (`run.py`): MarketContextCache, SignalFilter, RiskManager собираются и передаются в TradingBot.
- **Snapshot-покрытие analysis-слоя**: отдельный раннер `tests/snapshot/test_analysis.py` + собственные эталоны (`*_expected_context.csv`) на тех же общих свечах кейсов, покрывающие тренд и уровни S/R.

## Capabilities

### New Capabilities

- `market-analysis`: TrendAnalyzer и SRLevelsCalculator — вычисление рыночного контекста (направление/сили тренда и горизонтальные уровни S/R) по DataFrame свечей. Stateless frozen dataclass-калькуляторы с модульными параметрами.
- `market-context-cache`: Lazy-кэш рыночного контекста (MarketContext = TrendResult + SRLevels + current_price) по инструментам. Инвалидация при появлении новой закрытой свечи. Экономия: вычисление 1 раз на инструмент, а не на стратегию.
- `signal-filter`: Фильтрация Decision на основе тренда. Жёсткий фильтр: при противоречии тренда и сигнала → HOLD. Чистый трансформер (apply(decision, ctx) → Decision), не является ExecutionPort.
- `risk-management`: Расчёт SL/TP по уровням S/R. Для BUY: SL ниже ближайшего support, TP к ближайшему resistance. Для SELL — зеркально. Fallback 2% при отсутствии уровней.

### Modified Capabilities

- `orchestration`: Требование «Защита от дублирования сигналов» заменяется на «Уведомление при новой свече»: на пустом тике бот молчит, compute/decide не вызываются, уведомления не доставляются. Heartbeat также пропускается на пустом тике.
- `strategy-contract`: Требование «Тип решения без форматирования» дополняется: Decision расширяется обязательными полями SL/TP/trend с дефолтами None (обратная совместимость).
- `snapshot-testing`: Добавляется отдельный data-driven раннер для analysis-слоя (тренд + S/R), использующий те же общие `candles.csv` кейсов, но собственные эталоны `*_expected_context.csv` и не встречающийся с раннером стратегий. Существующий раннер стратегий не меняется.
- `test-layout`: Уточняется структура: analysis-эталоны хранятся рядом со стратегическими, но с отдельным суффиксом `_expected_context.csv`, чтобы не конфликтовать с паттерном `*_expected_signals.csv`.

## Impact

- **Новые файлы**: `src/analysis/__init__.py`, `src/analysis/models.py`, `src/analysis/trend.py`, `src/analysis/sr_levels.py`, `src/analysis/context_cache.py`, `src/analysis/filter.py`, `src/analysis/risk.py`; `tests/snapshot/test_analysis.py`
- **Изменённые файлы**: `src/strategies/contracts.py`, `src/bot/trading_bot.py`, `run.py`; `tests/snapshot/helper.py` (или вспомогательный helper для analysis-эталонов)
- **Изменённые спеки**: `openspec/specs/orchestration/spec.md`, `openspec/specs/strategy-contract/spec.md`, `openspec/specs/snapshot-testing/spec.md`, `openspec/specs/test-layout/spec.md`
- **Новые тестовые артефакты**: `tests/snapshot/data/<КЕЙС>/trend_expected_context.csv`, `tests/snapshot/data/<КЕЙС>/sr_levels_expected_context.csv` для существующих кейсов `BR_1h`, `NG_1h`
- **Зависимости**: `pandas_ta_classic` (для EMA/ADX) — уже есть; новых pip-зависимостей нет
- **Обратная совместимость**: все новые поля Decision имеют дефолт None; старые стратегии работают без изменений; фильтр/риск-менеджер опциональны (могут быть отключены через конфигурацию точки сборки)
- **Heartbeat**: текущий heartbeat считается по тикам; при пропуске пустых тиков heartbeat также пропускается — нужно будет считать по времени, если нужен мониторинг жизни бота между свечами
