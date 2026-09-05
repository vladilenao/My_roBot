## 1. Переименование src/analysis → src/market_context

- [x] 1.1 Выполнить `git mv src/analysis src/market_context`
- [x] 1.2 Обновить внутренние импорты в `src/market_context/` (`models.py`, `trend.py`, `sr_levels.py`, `context_cache.py`): `src.analysis.models` → `src.market_context.models` (включая локальный импорт в `context_cache.py:_empty_context`)
- [x] 1.3 Пересобрать `src/market_context/__init__.py`: исключить `SignalFilter` (из `filter.py`) и `RiskManager` (из `risk.py`) из реэкспорта и `__all__` — они переезжают в `decision/`

## 2. Вынос decision/

- [x] 2.1 Выполнить `git mv src/market_context/filter.py src/decision/filter.py` и обновить импорт на `from src.market_context.models import MarketContext, TrendDirection`
- [x] 2.2 Выполнить `git mv src/market_context/risk.py src/decision/risk.py` и обновить импорт на `from src.market_context.models import MarketContext, SRLevel, SRType`
- [x] 2.3 Создать `src/decision/__init__.py` с реэкспортом `SignalFilter` и `RiskManager`

## 3. Каркас market_structure/

- [x] 3.1 Создать `src/market_structure/__init__.py` с docstring-объявлением назначения дома («примитивы структуры рынка: swings, fibonacci, harmonic; модули добавляются в feature-change harmonic_abcd»), без модулей и поведения

## 4. Обновление потребителей

- [x] 4.1 `run.py`: блок `from src.analysis import (...)` разделить на `from src.market_context import (MarketContextCache, SRLevelsCalculator, TrendAnalyzer)` и `from src.decision import RiskManager, SignalFilter`
- [x] 4.2 `tests/snapshot/helper.py`: импорты `SRLevelsCalculator`, `TrendAnalyzer` перевести на `src.market_context`
- [x] 4.3 Перенести `tests/unit/analysis/test_trend.py`, `test_sr_levels.py`, `test_context_cache.py` → `tests/unit/market_context/` (`git mv`), обновить импорты на `src.market_context`
- [x] 4.4 Перенести `tests/unit/analysis/test_filter.py`, `test_risk.py` → новый `tests/unit/decision/`, обновить импорты: `src.analysis.filter` → `src.decision`, функции/модели — из `src.market_context.models`
- [x] 4.5 Удалить опустевший каталог `tests/unit/analysis/`

## 5. Правка доков-контекста

- [x] 5.1 `openspec/config.yaml:8`: заменить «Стратегия = пакет src/strategies/<имя>/ (strategy.py + indicators/ + signals/)» на «Стратегия = собственный файл `src/strategies/<имя>.py`, использующий общие `src/strategies/indicators/` (подпапки индикаторов) и `src/strategies/signals.py`; регистрируется в реестре `src.strategies` через `@register`»

## 6. Верификация

- [x] 6.1 Прогнать полный `pytest` (unit + snapshot) — зелёный, без изменения эталонов
- [x] 6.2 `rg "src\.analysis"` по репозиторию (исключая `openspec/changes/archive`) — ноль совпадений в коде и тестах
- [x] 6.3 Проверить зависимости слоёв: в `src/market_context/` ноль ссылок на `src.strategies.*`; `contracts` тянет только `src/decision/` (`filter.py`, `risk.py`) — по дизайну. Примечание: `import src.strategies` подтягивает `indicators.base` через `registry → strategy` — ПРЕДСУЩЕСТВУЮЩЕЕ поведение, стратегии не менялись; вопрос ленивости реестра вынесен в отдельное наблюдение (см. отчёт)