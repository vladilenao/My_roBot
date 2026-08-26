## Purpose

Извлекает реестр стратегий в отдельный модуль `registry.py` с auto-discovery через `pkgutil.iter_modules`, type hints и полной документацией.

## ADDED Requirements

### Requirement: Реестр в отдельном модуле
Реестр стратегий ДОЛЖЕН располагаться в `src/strategies/registry.py`. Модуль ДОЛЖЕН содержать: функцию `register`, декорирующую класс стратегии, функции доступа `get_strategy(name)`, `all_strategies()`, `strategy_names()`, и `validate_assignments()`.

#### Scenario: Импорт реестра
- **WHEN** любой модуль импортирует `src.strategies.registry`
- **THEN** импорт не вызывает загрузку пакетов стратегий или библиотеку индикаторов

### Requirement: Auto-discovery стратегий
Реестр ДОЛЖЕН использовать `pkgutil.iter_modules` для обнаружения пакетов стратегий в `src/strategies/` вместо хардкода импортов. Обнаружение ДОЛЖНО выполняться при первом обращении к функциям доступа реестра.

#### Scenario: Добавление новой стратегии без правки реестра
- **WHEN** создан новый пакет стратегии с файлом `__init__.py` и декоратором `@register`
- **THEN** стратегия автоматически обнаруживается реестром при первом обращении к функциям доступа

### Requirement: Type hints для реестра
Все функции реестра ДОЛЖНЫ иметь type hints:
- `register(cls: Type[T]) -> Type[T]`
- `get_strategy(name: str) -> Strategy`
- `all_strategies() -> list[Strategy]`
- `strategy_names() -> list[str]`
- `validate_assignments() -> None`

#### Scenario: Type hints доступны в IDE
- **WHEN** разработчик вызывает `get_strategy("macd_rsi_stoch")`
- **THEN** IDE показывает возвращаемый тип `Strategy`

### Requirement: `__all__` в `__init__.py`
Модуль `src/strategies/__init__.py` ДОЛЖЕН определять `__all__` со списком публичных символов.

#### Scenario: Импорт по star-import
- **WHEN** выполняется `from src.strategies import *`
- **THEN** импортируются только символы из `__all__`
