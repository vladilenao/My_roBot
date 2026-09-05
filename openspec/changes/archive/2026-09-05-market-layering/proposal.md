## Why

В `src/analysis/` смешаны две разные заботы: «чтение рынка» (тренд, уровни S/R, модель контекста) и «управление решением» (`SignalFilter`, `RiskManager`), которые зависят от `src.strategies.contracts`. Из-за этого `analysis` импортирует `strategies`, хотя рынок — фундамент и не должен знать о правилах торговли. Описание структуры стратегий в `openspec/config.yaml:8` («стратегия = пакет») противоречит спекам (`strategy-contract/spec.md:112`, `indicators/directory-structure`), по которым стратегия — это собственный файл плюс общие `indicators/` и `signals.py`; код соответствует спекам, а конфиг отстал.

Плюс под следующую стратегию (гармонический AB=CD) нужен нейтральный дом для примитивов структуры рынка (свинги, фибо, формации) вне `strategies/`, чтобы им могли пользоваться и стратегии, и контекст рынка.

## What Changes

- Создаётся пакет-каркас `src/market_structure/` — нейтральный дом для примитивов структуры рынка (`swings`, `fibonacci`, `harmonic`). Наполнение поведением — в следующем feature-change (стратегия `harmonic_abcd`), здесь только фиксируется имя и расположение дома.
- Из `src/analysis/` выносятся `filter.py` и `risk.py` в новый пакет `src/decision/` — «менеджмент решений» поверх `MarketContext` и `Decision`.
- `src/analysis/` переименовывается в `src/market_context/` (остаются `models.py`, `trend.py`, `sr_levels.py`, `context_cache.py`) — симметрия «карта + погода» с `market_structure/`.
- Обновляются все импорты потребителей (`trading_bot`, `run.py`, unit-тесты, snapshot-helper, tools).
- **BREAKING (внутренние пути импорта):** `src.analysis.*` → `src.market_context.*`; `src.analysis.filter` / `src.analysis.risk` → `src.decision.*`.
- `openspec/config.yaml:8` приводится в соответствие со спеками: стратегия = собственный файл `src/strategies/<имя>.py` + общие `indicators/` и `signals.py`.
- Внешнее наблюдаемое поведение бота не меняется.

## Capabilities

### New Capabilities

Нет: `src/market_structure/` создаётся как каркас без поведения, поведение появится в feature-change `harmonic_abcd`.

### Modified Capabilities

Нет: спеки описывают поведение, а не пути пакетов; поведение компонентов не изменяется, только их расположение. Это чистый рефакторинг + правка доков-контекста, поэтому в `.openspec.yaml` установлен `skip_specs: true`.

## Impact

- `src/analysis/`: `models.py`, `trend.py`, `sr_levels.py`, `context_cache.py` → переезжают в `src/market_context/`; `filter.py`, `risk.py` → в `src/decision/`; каталог `analysis/` удаляется.
- Создаётся `src/market_structure/` (сейчас — только `__init__.py`, резервация пространства имён).
- Обновляются импорты: `src/bot/trading_bot.py`, точка входа, `tests/unit/*`, `tests/snapshot/helper.py`, скрипты в `tools/`.
- `openspec/config.yaml` — правка строки 8 (контекст для AI), спеки не изменяются.
- Спеки (`openspec/specs/`) не изменяются.
- Связанные, НЕ входящие в этот change: стратегия `harmonic_abcd` + наполнение `market_structure/`, переезд `sr_levels` на общий `swings`, кластерный объём.