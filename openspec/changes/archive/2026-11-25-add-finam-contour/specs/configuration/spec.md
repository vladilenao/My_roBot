## ADDED Requirements

### Requirement: Настройки контура «Счёт Финам»
Нормативный уровень: MUST. Секция `[contours.finam]` ДОЛЖНА принимать те же ключи, типы и диапазоны, что `[contours.account]`, со значениями по умолчанию «Счёта», кроме:

| Ключ | По умолчанию |
|---|---|
| `telegram_bot_env` | `TELEGRAM_FINAM_BOT_TOKEN` |
| `telegram_channel_env` | `TELEGRAM_FINAM_CHANNEL_ID` |

Токен и номер счёта ДОЛЖНЫ читаться только из `FINAM_SECRET_TOKEN` и `FINAM_ACCOUNT_ID` и НЕ ДОЛЖНЫ задаваться в `robot.toml`. Вшитый `default.toml` ДОЛЖЕН содержать секцию со значениями по умолчанию и комментариями.

#### Scenario: Значения по умолчанию
- **WHEN** секции `[contours.finam]` в `robot.toml` нет
- **THEN** контур `finam` берёт бота из `TELEGRAM_FINAM_BOT_TOKEN`, канал из `TELEGRAM_FINAM_CHANNEL_ID`, сверяет счёт раз в минуту и работает только на чтение

#### Scenario: Номер счёта в конфиге
- **WHEN** `robot.toml` содержит `[contours.finam] account_id = "…"`
- **THEN** загрузка завершается `ConfigError`: незнакомый ключ

### Requirement: Секции контуров на брокере по описаниям
Нормативный уровень: MUST. Набор ключей, проверки и значения по умолчанию секции `[contours.<номер>]` контура на брокере ДОЛЖНЫ строиться по описанию контура: ключи — по виду, имена переменных бота и канала — из описания. У описания, адаптер которого не умеет заявки, `orders = true` ДОЛЖЕН завершать загрузку `ConfigError`: «[contours.<номер>] orders = true: адаптер этого контура пока не умеет заявок». Ключи, плоские имена и значения по умолчанию `[contours.account]` и `[contours.sandbox]` НЕ ДОЛЖНЫ меняться.

#### Scenario: Заявки на Финаме до Ф3
- **WHEN** `robot.toml` содержит `[contours.finam] orders = true`
- **THEN** загрузка завершается `ConfigError` с ключом `[contours.finam] orders` и причиной «адаптер этого контура пока не умеет заявок»

#### Scenario: «Счёт» и песочница прежние
- **WHEN** загружается `robot.toml` с прежними `[contours.account]` и `[contours.sandbox]`
- **THEN** значения настроек те же, что до изменения
