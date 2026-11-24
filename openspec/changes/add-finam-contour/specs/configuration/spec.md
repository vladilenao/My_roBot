## ADDED Requirements

### Requirement: Настройки контура «Счёт Финам»
Нормативный уровень: MUST. Секция `[contours.finam]` ДОЛЖНА принимать те же ключи, типы и диапазоны, что `[contours.account]`, со значениями по умолчанию «Счёта», кроме:

| Ключ | По умолчанию |
|---|---|
| `telegram_bot_env` | `TELEGRAM_FINAM_BOT_TOKEN` |
| `telegram_channel_env` | `TELEGRAM_FINAM_CHANNEL_ID` |

В этой версии `orders = true` ДОЛЖЕН завершать загрузку `ConfigError`: «[contours.finam] orders = true: в этой версии контур Финама только читает счёт». Токен и номер счёта ДОЛЖНЫ читаться только из `FINAM_SECRET_TOKEN` и `FINAM_ACCOUNT_ID` и НЕ ДОЛЖНЫ задаваться в `robot.toml`. Вшитый `default.toml` ДОЛЖЕН содержать секцию со значениями по умолчанию и комментариями.

#### Scenario: Значения по умолчанию
- **WHEN** секции `[contours.finam]` в `robot.toml` нет
- **THEN** контур `finam` берёт бота из `TELEGRAM_FINAM_BOT_TOKEN`, канал из `TELEGRAM_FINAM_CHANNEL_ID`, сверяет счёт раз в минуту и работает только на чтение

#### Scenario: Заявки на Финаме до Ф3
- **WHEN** `robot.toml` содержит `[contours.finam] orders = true`
- **THEN** загрузка завершается `ConfigError` с этим ключом и причиной «только читает счёт»

#### Scenario: Номер счёта в конфиге
- **WHEN** `robot.toml` содержит `[contours.finam] account_id = "…"`
- **THEN** загрузка завершается `ConfigError`: незнакомый ключ
