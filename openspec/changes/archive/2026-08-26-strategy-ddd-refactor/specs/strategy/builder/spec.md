## MODIFIED Requirements

### Requirement: StrategyBuilder — сборка конфигурации
`StrategyBuilder` ДОЛЖЕН предоставлять методы: `set_name(name)`, `set_strategy_window(window)`, `add_indicator(indicator)`, `build()`. Все setter-ы возвращают `self` для chaining.

#### Scenario: Полная сборка
- **WHEN** вызывается `StrategyBuilder().set_name("test").set_strategy_window(5).add_indicator(MacdIndicator(fast=12, slow=26, signal=9)).build()`
- **THEN** возвращается `StrategyConfig` с указанными параметрами

#### Scenario: Имя обязательно
- **WHEN** вызывается `StrategyBuilder().build()` без `set_name()`
- **THEN** выбрасывается `ValueError` с сообщением об обязательном имени

#### Scenario: Минимум один индикатор
- **WHEN** вызывается `StrategyBuilder().set_name("test").build()` без индикаторов
- **THEN** выбрасывается `ValueError` с сообщением о необходимости хотя бы одного индикатора

#### Scenario: Window положительный
- **WHEN** вызывается `StrategyBuilder().set_name("test").set_strategy_window(0).build()`
- **THEN** выбрасывается `ValueError` с сообщением о положительном окне

## REMOVED Requirements

### Requirement: Индикаторные Builder-ы
**Reason**: Избыточны при явной передаче параметров через frozen dataclass. Индикаторы создаются напрямую с обязательными параметрами.
**Migration**: Используйте `MacdIndicator(fast=12, slow=26, signal=9)` вместо `MacdIndicatorBuilder().set_fast(12).set_slow(26).set_signal(9).build()`.
