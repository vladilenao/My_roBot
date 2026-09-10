## 1. Переименование файлов (git mv)

- [x] 1.1 `git mv src/strategies/macd_rsi_stoch.py src/strategies/macd_rsi_stoch_strategy.py`
- [x] 1.2 `git mv src/strategies/flat_triangle.py src/strategies/flat_triangle_strategy.py`
- [x] 1.3 `git mv src/strategies/harmonic_abcd.py src/strategies/harmonic_abcd_strategy.py`
- [x] 1.4 `git mv src/strategies/strategy.py src/strategies/base_strategy.py`

## 2. Обновление импортов и реестра

- [x] 2.1 В переименованных файлах стратегий заменить `from src.strategies.strategy import StrategyConfig` на `from src.strategies.base_strategy import StrategyConfig`
- [x] 2.2 `src/strategies/registry.py`: импорт `StrategyConfig` из `base_strategy`; в skip-списке `_discover_strategies` заменить `"strategy"` на `"base_strategy"` и убрать `"base"`
- [x] 2.3 `run.py`: импорты `DEFAULT_CONFIG` из `macd_rsi_stoch_strategy` и `flat_triangle_strategy`
- [x] 2.4 `tools/download_snapshot_data.py`:
  - заменить импорт `StrategyConfig` на `from src.strategies.base_strategy import StrategyConfig`
  - обновить упоминания `src.strategies` в help и словаре конфигов созданных стратегий
- [x] 2.5 `tests/snapshot/test_strategies.py` и `tests/snapshot/helper.py`: импорты из новых имён модулей (`macd_rsi_stoch_strategy`, `flat_triangle_strategy`, `harmonic_abcd_strategy`, `base_strategy`); строки с именами стратегий в словарях/диспатчах НЕ менять (это `NAME`)
- [x] 2.6 `tests/unit/strategies/test_registry.py`, `test_signals.py`, `test_strategy_builder.py`, `test_harmonic_abcd.py`: импорты из новых имён модулей; строка «лёгкости импорта» обновляется на `src.strategies.macd_rsi_stoch_strategy`

## 3. Проверки

- [x] 3.1 `grep -rn "strategies\.macd_rsi_stoch\|strategies\.flat_triangle\|strategies\.harmonic_abcd\|strategies\.strategy" --include="*.py" src tests tools run.py` — ровно 0 совпадений (кроме `config.py`/`names.py` с идентификаторами)
- [x] 3.2 Полный `pytest` — все тесты зелёные (347 + без регрессий)
- [x] 3.3 `openspec validate --changes` — change валиден

## 4. Фиксация конвенции

- [x] 4.1 Обновить `openspec/config.yaml` (контекст): «Стратегия = собственный файл `src/strategies/<имя>.py>` → `src/strategies/<имя>_strategy.py>` + уточнение про инфраструктурные модули (`base_strategy.py` и др.)»
- [x] 4.2 При архивации перенести дельту `strategy-contract` в main-спек