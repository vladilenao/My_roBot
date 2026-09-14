## Context

- Бот циклически обрабатывает закрытые свечи по набору активных ТФ; на каждом тике вызываются `_process` → `_analyze` (`src/bot/trading_bot.py:177`), где базовая стратегия выдаёт `Decision`, затем `SignalFilter.apply()` применяет профиль, и `RiskManager` управляет позицией.
- Существующие фильтры (`basic_levels`, `raw`) stateless, не используют рыночные данные или индикаторы; `ProfileFilter.apply(decision, ctx)` — чистый трансформер (`src/decision/filters/base.py:14`).
- Реестр фильтров — словарь `PROFILES` с единичными экземплярами (`src/decision/filters/__init__.py:11`).
- `MarketDataCache.frame_for()` лениво загружает историю на 30 дней от первого вызова (`src/data/loader.py:37`), инкрементально дозагружает только новые закрытые бары; инкремент обновления выполняется только для активных ТФ (вызывается из `refresh_if_new_candle` на тике).
- `MultiTimeframeScheduler.grid(tf)` выбрасывает `KeyError` для ТФ, не входящих в `ACTIVE_TIMEFRAMES` (`src/scheduler/timing.py:221`).
- Иерархии Элдера M5→M30→H4, M15→H1→H4, M30→H4→1D уже валидны при расширенной лестнице; невалидным является только выход за верх (например, M1×5=5M → нет стандартного ТФ строго больше, 1M×5 → нет).
- Лестница TIMEFRAMES сейчас: `1m,5m,15m,1h,1d,1w,1M`; лестница `_PERIODS` идентична (`src/scheduler/timing.py:13`). Таймфреймы `30m` и `4h` поддерживаются API (`t_tech.invest.CandleInterval.CANDLE_INTERVAL_30_MIN / _4_HOUR`), но отсутствуют в конфигурации.
- Индикаторы MACD и Stochastic реализованы как frozen dataclass с `compute(df)` → enriched DataFrame, и свойством `warmup` (`src/strategies/indicators/macd/indicator.py:34`, `src/strategies/indicators/stochastic/indicator.py:19`).

## Goals / Non-Goals

**Goals:**
- Новый профиль `triple_screen` с тремя экранами: направление (MACD hist на `TF_htf`), коррекция (%K на `TF_int`), вход (базовая стратегия). Воспроизведение методики Элдера.
- Детерминированная иерархия таймфреймов по множителю (дефолт 5) в `time_hierarchy()` с вычислением при старте и валидацией на этапе загрузки конфигурации (`ConfigError`).
- Кадры `TF_htf`/`TF_int` доступны через адаптер `HtfFrameProvider`, привязанный к `MarketDataCache`; адаптер выполняет: (a) lazy загрузку/инкремент, (b) time-align (`bar_close <= close_work`), (c) минимальную глубину (previous-session coverage), (d) исключение из множества активных ТФ ритма.
- В config_loader.py появляется валидация привязок `triple_screen`: вычисление `tf_hierarchy`, проверка наличия в TIMEFRAMES, `ConfigError` при невалидной.
- Зависимости фильтра (`HtfFrameProvider`, конфиг) передаются в конструктор `TripleScreenFilter` в `run.py`; фасад `SignalFilter` получает `instrument` и `timeframe` в `apply()`.
- При холодном старте (недостаточно закрытых свечей текущей сессии) расчёт использует предыдущую торговую сессию; при невозможности даже с ней — HOLD с предупреждением в `bot_debug.log`.

**Non-Goals:**
- Статистика / дашборд профиля ( counters blocked/rejected, etc. )
- Автоматический тюнинг множителя и порогов
- Агрегация результатов по нескольким профилям / усложнение базовых стратегий
- Изменение доставки / уведомлений сверх текущего шаблона «Отклонено фильтром»

## Decisions

### 1. Иерархия ТФ: один чистый источник `tf_hierarchy(work, multiplier)`, расчёт дважды

Расчёт иерархии живёт в модуле `src/decision/filters/triple_screen.py` как чистая функция, зависящая только от лестницы TIMEFRAMES и helper `tf_period_minutes()` из `src/scheduler/timing.py`. Функция используется в двух контекстах:
- На этапе загрузки конфигурации (`config_loader.py`) — валидация привязок `triple_screen`, `ConfigError` при невалидном ТФ.
- В фильтре (runtime) — вычисление пары `(TF_int, TF_htf)` при обработке тика.

**Альтернативы:**
- Вычислять и кэшировать в config_loader, передавать в фильтр через конфиг-объект — избыточно, связывает конфиг с контекстом привязки, сложнее тестировать.
- Расчёт в timing.py — timing слой не должен знать об Элдере.

`tf_period_minutes()` в `timing.py` возвращает точную длительность ТФ в минутах (для 1M ≈ 43200). Иерархия: `tf_int` = минимальный `tf` из TIMEFRAMES строго больше `work_min * multiplier`; аналогично `tf_htf` от `int_min * multiplier`.

### 2. Работа с кадрами старших ТФ: адаптер `HtfFrameProvider`, а не расширение фасада фильтра

Фильтр получает зависимость `data_provider: HtfFrameProvider` через конструктор. Адаптер оборачивает `MarketDataCache` + `MultiTimeframeScheduler`:

```python
class HtfFrameProvider(Protocol):
    def frame_for(self, instrument, tf, *, max_close: datetime, min_bars: int) -> pd.DataFrame: ...
```

Адаптер-реализация (`src/data/htf_provider.py`):
1. `ensure_fresh_frame(instrument, tf)` — public-метод, проверяет `self._cache._observed` и при необходимости вызывает incremental `_load()` + merge для on-demand TF (использует внутренности кэша; для чистоты добавить public `ensure_loaded` метод в `MarketDataCache`).
2. `alignment` — срезает строки с `grid.bar_close(datetime) > max_close`.
3. `min_bars check` — если в срезе < `min_bars`, добавляет предупреждение в `bot_debug.log` и возвращает то, что есть (фильтр сам решает HOLD по длине кадра).

**Альтернативы:**
- Метод `frame_sliced_for()` прямо в `MarketDataCache` — громоздко, добавляет выравнивание (alignment) в общий кэш, который не использует это для активных ТФ; сложнее поддерживать.
- Расчёт/обрезка в самом фильтре — фильтр не должен знать про сетки и инкрементальную загрузку (нарушение SRP).

**Тёплый старт (нормальная работа):** первый вызов `frame_for` для неактивного TF лениво загружает 30 дней, что перекрывает несколько недель; alignment обрезает будущее. Неактивный TF не попадает в `_grids`, `next_boundary` и `fallback_secs` не затронуты.

### 3. Warmup и расчёт индикаторов: переиспользование `MacdIndicator` и `StochasticIndicator`

После получения выровненного кадра `tf_htf` вычисляем:
```python
macd = MacdIndicator(fast=..., slow=..., signal=...)
data = macd.compute(htf_frame)
hist = data[f"macdh_{fast}_{slow}_{signal}"]
direction_up = hist.iloc[-1] > hist.iloc[-2]
```

Для `TF_int`:
```python
stoch = StochasticIndicator(k=..., d=..., smooth_k=...)
data = stoch.compute(int_frame)
k_val = data[f"stochk_{k}_{d}_{smooth_k}"].iloc[-1]
```

Минимальное число закрытых свечей (для проверки в фильтре):
- `htf_frame`: ≥ `macd.warmup + 2` (первое валидное Hist[t-1] + Hist[t]).
- `int_frame`: ≥ `stoch.warmup + 1` (первое валидное %K[t]).

Индикаторы frozen dataclass, не мутабельны; `compute()` вызывается на неизменённом копии кадра. Warmup требует правильного количества строк; фильтр проверяет `len >= required_warmup` и при нехватке (включая проверку предыдущей сессии) → HOLD + warning в `bot_debug.log`.

### 4. Подключение фильтра и передача инструмента/ТФ

**`src/decision/filter.py`:** сигнатура `apply()` расширяется:
```python
def apply(self, decision, ctx, profile_name=DEFAULT_FILTER_PROFILE, instrument="", timeframe="") -> Decision
```
Для существующих профилей `instrument`/`timeframe` игнорируются; `TripleScreenFilter` использует их для `data_provider.frame_for(...)`.

**`src/bot/trading_bot.py:195`:** текущий вызов
```python
decision = self._signal_filter.apply(decision, context, profile_name=assignment.filter_profile)
```
дополняется `instrument=instrument, timeframe=tf`. Передаётся **объект `Instrument`** (а не `instrument.label`): `HtfFrameProvider` резолвит кадры через `MarketDataCache._key`, которому нужны `ticker`/`instrument_type`; отображаемый `label` — display-строка, непригодная для поиска инструмента в кэше. Существующие профили (`raw`, `basic_levels`) игнорируют `instrument`.

**`run.py`:** создаётся `HtfFrameProvider(data_cache, timeline)`, затем `TripleScreenFilter(provider=..., params=TripleScreenParams(...))`; регистрируется в `PROFILES`.

### 5. Регистрация фильтра и порядок составления

`TripleScreenFilter` — не singleton. `PROFILES` по-прежнему словарь с экземплярами, но `triple_screen` экземпляр конструируется в `run.py` с передачей `data_provider` и параметров из конфига. Регистрация:
```python
from src.decision.filters import PROFILES
from src.decision.filters.triple_screen import TripleScreenFilter
PROFILES["triple_screen"] = TripleScreenFilter(provider=htf_provider, params=params)
```
Это выполняется после инициализации `data_cache` и `timeline` в `run.py`. SignalFilter._PROFILES ClassVar берёт словарь из модуля; изменение словаря после импорта отражается (late binding на словарь).

Альтернативы:
- Singleton + передача провайдера в `apply()` — нарушает контракт «filtered чистый трансформер»; фильтр должен содержать зависимость, а не получать её на каждый вызов.
- Фабрика в сигнатуре фильтра — избыточно, затрудняет тестирование (много параметров).

### 6. Конфигурация triple_screen и валидация при загрузке

Новая TOML-секция `[strategies.filter.triple_screen]` (или неявный дефолт `default.toml`). Параметры:
- `multiplier` (int, ≥2, default 5)
- `macd_fast/slow/signal` (int, default 12/26/9)
- `stoch_k/d/smooth_k` (int, default 14/3/3)
- `oversold/overbought` (int, default 20/80)

Валидация:
- Незнакомые ключи → `ConfigError` (режим строгой валидации `config_loader._SECTIONS`).
- Для каждой привязки с `filter = "triple_screen"` вычисляется `tf_hierarchy(assignment.tf, multiplier)`; если `TF_int` или `TF_htf` не в TIMEFRAMES → `ConfigError` с указанием привязки.
- Параметры структуры хранятся в `triple_screen.py` как `TripleScreenParams` dataclass; десериализация и валидация — часть `triple_screen.py` (не config_loader), чтобы валидационная логика оставалась рядом с алгоритмом.

## Risks / Trade-offs

- **[Risk] Связь decision ↔ config_loader (импорт triple_screen в config_loader).**
  *Mitigation:* граница — чистая функция `tf_hierarchy` и dataclass `TripleScreenParams` без副作用 и без зависимости от MarketDataCache/timeline; легко тестировать; config_loader остаётся outside-in. Поддерживаемо.

- **[Risk] Warmup frame guard: точное число баров, доступных при разных режимах (MOEX session, праздники, 24h instruments).**
  *Mitigation:* warmup требует few dozen баров; 30-дневное окно загрузки перекрывает любые календарные сценарии; фильтр всё равно проверяет `len(frame) >= required_warmup`, HOLD при нехватке. Автотест проверяет граничные (неделя/выходной) и холодный старт.

- **[Risk] Never refreshed on-demand TF: стагнация кадра, MACD hist не обновляется, HtfFrameProvider использует stale данные (пусто или old).**
  *Mitigation:* адаптер выполняет `ensure_loaded` на каждом вызове; для неактивного TF增量 загрузка недоступна (boundary не проверяется), но alignment slicing на `max_close` (close_work) гарантирует актуальность данных к моменту свечи; новые закрытые бары появятся при следующем тике, где `frame_for` возвращает актуальные данные, т.к. кадр строится по API на основе max_close. Реально: первый вызов загружает 30 дней, что всегда покрывает прошлые сессии;以後增量 загрузка не требуется (style frame is immutable per tick).

- **[Risk] Конфигурация > default.toml: robot.toml может не содержать `[strategies.filter.triple_screen]` — пользователь verwart.**
  *Mitigation:* дефолты в `TripleScreenParams` и `default.toml` фиксируют работающий профиль; documented «при отсутствии секции — дефолты Элдера (×5, MACD 12/26/9, Stochastic 14/3/3, 20/80)».

- **[Trade-off] Пользователь увидит «Отклонено фильтром» при cold-start HOLD** — может сбить с толку в первые часы.
  *Mitigation:* новая сценарная обработка в `user_error_message` / `bot_debug.log`: distinction «отклонено (недостаточно истории старших ТФ)»; в уведомлении звучит как «Отклонено фильтром» — допустимо, в логе — детали. Не ломает текущий UX.

## Migration Plan

- Ветка `feature/add-triple-screen-filter`: изменения не ломают существующие стратегии и фильтры.
- Фаза 1: добавление TIMEFRAMES (`30m`, `4h`) + `_PERIODS` + настройка timing grids — обратно-совместимо.
- Фаза 2: config_loader валидация + настройка `default.toml` + sections.
- Фаза 3: `HtfFrameProvider` + интеграция с `MarketDataCache` (ensure_loaded).
- Фаза 4: TripleScreenFilter + triple_screen.py + индикаторы + тесты.
- Фаза 5: подключение в `run.py` + обновление `SignalFilter.apply()` + `trading_bot.py`.
- Откат: revert ветки (changes без side effects). Rollback: откатить ветку,重返 v2.1.1.

## Open Questions

- Определение «торговая сессия предыдущего дня»: текущий загрузчик не фильтрует по session; загрузка 30 дней просто возвращает все свечи. Если инструмент торгуется 24/7, conceptually предыдущая сессия = вчерашние бары; для MOEX (07:00-23:50 UTC) предыдущая сессия = дневные бары за вчерашний календарный день, без ночных (т.к. свечи отсутствуют). Фильтр не различает — он получает все свечи за 30 дней с alignment по `bar_close ≤ close_work`. Расчёт «учёта предыдущей сессии» garantiazza automatic得益于 30-day загрузки. Вопрос к будущему: нужна ли фильтрация по session-aware границам (ночь, праздники) при вычислении индикаторов? Сейчас — нет, не нужна, т.к. свечи за праздники отсутствуют, и warmup guard (len ≥ required) покрывает edge cases.