## ADDED Requirements

### Requirement: Комиссия в процентах от стоимости контракта
Нормативный уровень: MUST. `[trading.risk_limits]` и секции `[contours.emulation]`, `[contours.sandbox]`, `[contours.account]` ДОЛЖНЫ принимать ключ `commission_pct` — комиссию за одну сторону в процентах от стоимости контракта, число от 0 до 1. Значение вне диапазона, нечисло или `bool` ДОЛЖНЫ завершать загрузку `ConfigError` с указанием секции и ключа.

Контур ДОЛЖЕН брать процент своей секции, без неё — `[trading.risk_limits] commission_pct`. Прогоны истории ДОЛЖНЫ брать общий процент. Если процент не задан нигде, ДОЛЖНА действовать `[trading.risk_limits] commission` в рублях за контракт.

Вшитый `default.toml` ДОЛЖЕН задавать `[trading.risk_limits] commission_pct = 0.025` и `[contours.sandbox] commission_pct = 0.05`.

#### Scenario: Свой процент песочницы
- **GIVEN** `[trading.risk_limits] commission_pct = 0.025`, `[contours.sandbox] commission_pct = 0.05`
- **WHEN** загружаются настройки
- **THEN** песочница считает комиссию по 0,05%, эмуляция, «Счёт» и история — по 0,025%

#### Scenario: Опечатка
- **WHEN** `robot.toml` содержит `[contours.account] commission_pct = 2.5`
- **THEN** загрузка завершается `ConfigError` с указанием `[contours.account] commission_pct`

#### Scenario: Процента нет
- **GIVEN** `commission_pct` не задан ни в одной секции, `commission = 1.5`
- **WHEN** строится план сделки
- **THEN** комиссия плана — 1,5 ₽ за контракт, как до изменения
