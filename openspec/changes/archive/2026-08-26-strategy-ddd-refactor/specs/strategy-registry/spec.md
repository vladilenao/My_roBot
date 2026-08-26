## MODIFIED Requirements

### Requirement: Реестр в отдельном модуле
Реестр стратегий ДОЛЖЕН располагаться в `src/strategies/registry.py`. Модуль ДОЛЖЕН содержать: функцию `register`, декорирующую класс стратегии, функции доступа `get_strategy(name, config)`, `all_strategies()`, `strategy_names()`, и `validate_assignments()`. Функция `get_strategy` ДОЛЖНА принимать обязательный параметр `config: StrategyConfig` и передавать его в конструктор стратегии.

#### Scenario: Импорт реестра
- **WHEN** любой модуль импортирует `src.strategies.registry`
- **THEN** импорт не вызывает загрузку пакетов стратегий или библиотеку индикаторов

#### Scenario: Получение стратегии с конфигом
- **WHEN** вызывается `get_strategy("macd_rsi_stoch", config=some_config)`
- **THEN** возвращается экземпляр стратегии, созданный с переданным конфигом

#### Scenario: Config обязателен
- **WHEN** вызывается `get_strategy("macd_rsi_stoch")` без второго аргумента
- **THEN** выбрасывается `TypeError` (отсутствует обязательный аргумент config)
