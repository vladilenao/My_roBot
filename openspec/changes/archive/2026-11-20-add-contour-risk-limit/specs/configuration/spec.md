## ADDED Requirements

### Requirement: Риск портфеля контура
Нормативный уровень: MUST. Секции `[contours.emulation]`, `[contours.sandbox]` и `[contours.account]` ДОЛЖНЫ принимать ключ `portfolio_pct` — число от 0 до 100, процент капитала контура, который могут потерять все его открытые остатки и ожидающие входы вместе. Торговая часть контура ДОЛЖНА считать бюджет риска портфеля от этого процента. Если ключа нет, ДОЛЖЕН действовать `[trading.risk_limits] portfolio_pct`. Прогоны истории ДОЛЖНЫ использовать общий `portfolio_pct`.

Значение вне диапазона, не число или `bool` ДОЛЖНЫ завершать загрузку `ConfigError` с указанием секции и ключа. `[trading.risk_limits] min_trade_risk_pct` больше `portfolio_pct` контура ДОЛЖЕН завершать загрузку `ConfigError`.

#### Scenario: Свой риск у «Счёта»
- **GIVEN** `[trading.risk_limits] portfolio_pct = 2`, `[contours.account] portfolio_pct = 6`, капитал счёта 9 900 ₽
- **WHEN** сигнал «Счёта» с риском одного лота 528 ₽
- **THEN** бюджет риска «Счёта» 594 ₽, вход на 1 лот; у песочницы и эмуляции бюджет считается от 2%

#### Scenario: Ключа нет
- **WHEN** в `[contours.sandbox]` нет `portfolio_pct`
- **THEN** песочница считает бюджет риска от `[trading.risk_limits] portfolio_pct`

#### Scenario: Вне диапазона
- **WHEN** `robot.toml` содержит `[contours.emulation] portfolio_pct = 150`
- **THEN** загрузка завершается `ConfigError` с указанием `[contours.emulation] portfolio_pct`
