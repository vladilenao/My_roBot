## Why

В каталоге `src/strategies/` рядом со стратегиями лежат инфраструктурные модули (`registry.py`, `strategy.py`, `signals.py`, `names.py`, `contracts.py`, `indicators/`), из‑за чего имя файла `macd_rsi_stoch.py` не даёт понять, что это файл именно стратегии, а `strategy.py` путается с этим же понятием. Имя файла должно явно нести роль модуля.

## What Changes

- **BREAKING**: переименование модулей стратегий в `src/strategies/` по единому правилу `<имя>_strategy.py`:
  - `macd_rsi_stoch.py` → `macd_rsi_stoch_strategy.py`
  - `flat_triangle.py` → `flat_triangle_strategy.py`
  - `harmonic_abcd.py` → `harmonic_abcd_strategy.py`
- **BREAKING**: `src/strategies/strategy.py` (общая инфраструктура: `StrategyConfig`, `StrategyBuilder`) переименовывается в `src/strategies/base_strategy.py` — суффикс `_strategy` закрепляется за файлами самих стратегий, а не за инфраструктурой.
- Обновляются все импорты во `src/`, `tests/`, `tools/download_snapshot_data.py`, `run.py`.
- Реестр (`registry.py` `_discover_strategies`): в список исключений вместо `"strategy"` попадает `"base_strategy"`; переименованные файлы стратегий по-прежнему обнаруживаются `pkgutil.iter_modules` (имя модуля заканчивается на `_strategy`, но не совпадает с исключением).
- Имена регистрации (`NAME`, `StrategyName`, привязки в `config.py`) остаются прежними: `macd_rsi_stoch`, `flat_triangle`, `harmonic_abcd` — меняется только имя файла/модуля, не идентификатор стратегии.
- Конвенция фиксируется: файл стратегии — `src/strategies/<имя>_strategy.py`, инфраструктура — `src/strategies/base_strategy.py`, `registry.py`, `signals.py`, `names.py`, `contracts.py`, `indicators/`.

## Capabilities

### New Capabilities
- (нет)

### Modified Capabilities
- `strategy-contract`: меняется требование «Изолированный пакет стратегии» — правило именования файла стратегии становится `src/strategies/<имя>_strategy.py` вместо `src/strategies/<имя>.py`; добавляются сценарии про различение файла стратегии и инфраструктуры. Дельту получить из существующего main-спека.

## Impact

- `src/strategies/macd_rsi_stoch_strategy.py`, `flat_triangle_strategy.py`, `harmonic_abcd_strategy.py`, `base_strategy.py` — новые имена файлов (git mv).
- Импорты обновляются: `src/strategies/registry.py`, `run.py`, `tools/download_snapshot_data.py`, `tests/snapshot/{helper,test_strategies}.py`, `tests/unit/strategies/{test_registry,test_signals,test_strategy_builder,test_harmonic_abcd}.py`.
- `openspec/config.yaml` (контекст проекта): строка про «Стратегия = собственный файл» получает правило `<имя>_strategy.py`.
- Реестр: skip-список в `registry.py`; поведение автообнаружения не меняется (проверяется тестами `test_discover_strategies*` и `test_literal_names_match_registry`).
- Мёртвый `# base` в skip-списке реестра можно убрать заодно (модуля `base.py` давно нет).