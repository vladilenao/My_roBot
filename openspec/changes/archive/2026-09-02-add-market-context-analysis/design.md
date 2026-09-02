## Context

Бот (Tinkoff Invest API + pandas_ta_classic) строит решения стратегий независимо от рыночного контекста. Текущая архитектура: `TradingBot._tick()` → `_process()` → `_analyze()` (per strategy: `compute` → `decide`) → `_emit()` → `ExecutionPort`. `Decision` — frozen dataclass без SL/TP/trend полей. `MarketDataCache` хранит свечи и дозагружает только новые бары (инкрементально). Мотивация — см. proposal.md «Why».

## Goals / Non-Goals

**Goals:**
- Разделить «контекст рынка» (тренд + S/R) от «решения стратегии»
- Избежать дублирующих вычислений: контекст считается 1 раз на инструмент, не на стратегию
- Пропускать пустые тики целиком (не гонять pipeline при 3600 опросах/час)
- Сохранить полную обратную совместимость (новые поля Decision с дефолтом None)

**Non-Goals:**
- Реальная торговля ордерами/стопами (ExecutionPort остаётся уведомляющим; BrokerPort — будущая работа)
- Оптимизация пересчёта индикаторов (compute) — это не узкое место при 1h; контекст дороже по смыслу, индикаторы дешевле
- Градация confidence вместо жёсткого блока — принято жёсткое решение
- Мягкий (пороговый) фильтр тренда

## Decisions

### 1. Context вычисляется 1 раз на инструмент через MarketContextCache, а не на стратегию

Rationale: `TrendAnalyzer.analyze(df)` и `SRLevelsCalculator.compute(df)` зависят только от DataFrame инструмента, не от стратегии. Для инструмента с 3 стратегиями расчёт 1× вместо 3×. Ленивое вычисление + инвалидация по `datetime` последней свечи (сравнение tz-naive, согласованное с MarketDataCache).

Alternatives: кэшировать в MarketDataCache (нарушение SRP — он не должен знать о тренде); хранить в TradingBot (оркестратор превращается в кэш).

### 2. SignalFilter и RiskManager — чистые трансформеры `apply(decision, ctx) → Decision`, НЕ декораторы ExecutionPort

Rationale: Декорация ExecutionPort смешивает «модификацию решения» и «доставку» (leaky abstraction) и заставляет порт нести семантику фильтрации. Чистые трансформеры изолированы, тестируемы, не зависят от data_cache (получают уже готовый MarketContext). Каждый — одна ответственность.

### 3. Жёсткий фильтр тренда (несоответствие → HOLD), а не порог confidence

Rationale: Простота и предсказуемость. При противоречии тренд-сигналу блокируем. Сила фильтрации (граница «насколько сильный тренд нужен, чтобы блокировать») откладывается.

Trade-off: теряется информация стратегии, которая могла видеть глубокую перепроданность. Рассмотрено, но принято простое поведение; гибкость можно добавить позже параметром confidence_threshold.

### 4. SL/TP по уровням S/R с fallback 2%

Rationale: Стопы по реальным рыночным зонам (ниже support / выше resistance) дают объективный риск/вознаграждение, а не случайный процент. Fallback 2% нужен, когда уровней нет. Risk:reward = 2.0 по умолчанию.

### 5. Пропуск пустых тиков

Rationale: Бот опрашивает данные каждые 1с (TICK_POLL_SECS), свеча закрывается раз в 1h. Без пропуска — полный pipeline ~3600 раз/час на одних и тех же данных. `_tick()` проверяет `has_fresh_closed_bar()` → False ⇒ `return` без `_process`, без heartbeat.

Trade-off: уведомление HOLD на каждом тике и heartbeat внутри свечи теряются. Heartbeat становится time-based при необходимости мониторинга — см. Risks.

### 6. Расширение Decision, а не новый тип

Rationale: Добавляем поля SL/TP/trend с дефолтами None — обратная совместимость, существующие стратегии не меняют `decide()`. Обогащение выполняют фильтр/риск-менеджер через `dataclasses.replace`.

## Risks / Trade-offs

- [Потеря info-confidence при жёстком блоке] → Mitigation: поведение простое и детерминированное; порог можно добавить позже параметром, не меняя кода стратегий.
- [Heartbeat пропускается внутри свечи] → Mitigation: если нужен мониторинг жизни между свечами — перевести heartbeat на time-based (не в этой смене). Зафиксировано как Open Question.
- [Разрастание конфигурации: 8+ параметров в 3 модулях] → Mitigation: параметры задаются через конструкторы frozen dataclass с дефолтами; при необходимости — per-instrument конфиг позже.
- [Кэш контекста устаревает, если свеча пересчитана без новой метки] → Mitigation: инвалидация только по `datetime` последней строки; согласовано с `_last_loaded` в MarketDataCache.

## Migration Plan

1. Расширить `Decision` (компилируется, обратная совместимость).
2. Ввести `src/analysis/` модули (новые, изолированы — ничего не ломают).
3. Изменить `TradingBot` (пропуск пустых тиков, интеграция пайплайна).
4. Обновить `run.py` (композиция).
5. Откат: при проблемах отключить фильтр/риск-менеджер в `run.py`, вернуть старый `_tick`, убрать новые поля — старые стратегии работают как раньше.

## Open Questions

- Нужен ли heartbeat между свечами (time-based) для мониторинга живости бота? Решается позже без изменения спек/задач.
- Нужны ли SL/TP в тексте уведомления? Решается позже, форматтер вне scope.

## Snapshot-тесты analysis-слоя

Analysis-слой (тренд, S/R) покрывается отдельным data-driven раннером `tests/snapshot/test_analysis.py`, использующим те же общие `candles.csv` кейсов, что и раннер стратегий, но собственные эталоны `*_expected_context.csv`. Разделение раннеров выбрано потому, что analysis контрактно отличается от стратегий: нет `Strategy`-контракта, `compute/decide/expected_events`, разные столбцы и объекты (`TrendResult`, `SRLevel`), а не `Decision`. Смешивать их в одном раннере нарушило бы спек `test-layout` и контракт стратегий.

```
    tests/snapshot/
    ├── test_strategies.py   # стратегии: compute → decide      (существующий)
    └── test_analysis.py     # analysis: trend + S/R            (новый)
    └── data/<КЕЙС>/
        ├── candles.csv                        # общие свечи
        ├── <strategy>_expected_signals.csv    # эталоны стратегий
        └── trend_expected_context.csv         # эталон тренда    (новый)
        └── sr_levels_expected_context.csv     # эталон S/R       (новый)
```

Ключевые решения:
- **Сумффикс `_expected_context.csv`** отделяет analysis-эталоны от стратегических `*_expected_signals.csv`, чтобы раннер стратегий их не подхватывал, и наоборот.
- **Диспетчеризация по префиксу имени файла** (`trend_`, `sr_levels_`) определяет, какой модуль анализа прогоняем — аналогично тому, как стратегии диспетчеризуются реестром по имени.
- **Тот же механизм `--update-snapshots`** из `tests/conftest.py` — регенерация эталонов при изменении логики.
- **Сравнение** через `assert_frame_equal` (rtol), указание первой расходящейся строки — единообразно с раннером стратегий.
- **Детерминизм**: результат зависит только от фикстуры, модуля analysis и версий библиотек.

Ручная проверка написана в разделе «Manual Verification Guide»; автоматическая (CI/pytest) — в этой.

## Manual Verification Guide

Ручная проверка реализации по спеке. Бóльшая часть покрывается быстрым pytest + ipython без реальной торговли; live smoke нужен только для orchestration.

### Быстрый полный прогон

```bash
pytest tests/unit -v             # вся новая логика
pytest tests/snapshot -v         # стратегии не сломаны
ruff check src/analysis          # линт новых модулей
```

### market-analysis (тренд + S/R) — ipython + реальные свечи

```python
from src.data.loader import load_candles
from src.analysis.trend import TrendAnalyzer
from src.analysis.sr_levels import SRLevelsCalculator
from src.config import TINKOFF_TOKEN

df, _ = load_candles("SBER", "share", "1h", token=TINKOFF_TOKEN)
result = TrendAnalyzer().analyze(df)            # direction, strength 0..1
price = df["close"].iloc[-1]
levels = SRLevelsCalculator().compute(df, price) # sorted, strength>=min_touches, <=max_levels
```

Смысловая проверка: нарисуй те же свечи в Excel/таблицах и сверь найденные уровни с очевидными фрактальными разворотами — уровни должны попадать в зоны, где цена реально отталкивалась.

### market-context-cache — fake MarketDataCache (без API)

```python
from src.analysis.context_cache import MarketContextCache

class FakeCache:
    def __init__(self, df): self.df = df
    def frame_for(self, inst): return self.df

cache = MarketContextCache(FakeCache(df_t1), TrendAnalyzer(), SRLevelsCalculator())
inst = ("SBER", "share")
c1 = cache.context_for(inst)
c2 = cache.context_for(inst)
assert c1 is c2                # тот же объект, пересчёта нет

cache2 = MarketContextCache(FakeCache(df_t2), ...)  # новая свеча
assert cache2.context_for(inst) is not c1           # пересчёт
```

Ключевой индикатор: `c1 is c2` — без пересчёта возвращается тот же объект. Можно временно добавить счётчик в `_compute`, чтобы увидеть 1 вызов на 3 стратегии.

### signal-filter — таблица решений

```
    тренд  сигнал  результат
    up     BUY     BUY (проходит)
    up     SELL    HOLD (блок)
    down   BUY     HOLD (блок)
    down   SELL    SELL (проходит)
    flat   BUY     BUY (проходит)
    flat   SELL    SELL (проходит)
    любой  HOLD    HOLD (не фильтруется)
```

Проверить через `SignalFilter().apply(Decision, ctx)`: при блоке `signal_type == HOLD` и `trend_confidence == 0.0`; при проходе `trend_direction`/`trend_confidence` из ctx.

### risk-management — ручные сценарии

- BUY при support=97, resistance=105, цена=100 → SL≈97 (`"S1"`), TP=106 (2× риска), label проставлены.
- Нет уровня в нужном направлении → SL = price ± 2%, label = None.
- Пустой список уровней → fallback 2%.
- Вход HOLD → без изменений.

### orchestration — live smoke (пропуск пустых тиков)

```
    NOTIFIER=console, TIMEFRAME=1m  python run.py

    после запуска:  1 тик (bootstrap) → 1 решение
    следующие 59 сек: НИЧЕГО (пустые тики, тишина)
    на след. свече:  снова 1 решение
```

Это и есть проверка «пустой тик = тишина». Таймфрейм `1m` даёт быстрый цикл; для реальной эксплуатации используется `1h`.

### strategy-contract — регрессия

```python
d = strategy.decide(ta)
assert d.stop_loss is None and d.take_profit is None  # стратегия сама не заполняет
```

Плюс весь `pytest` — старые стратегии возвращают Decision с None-полями и работают без ошибок.
