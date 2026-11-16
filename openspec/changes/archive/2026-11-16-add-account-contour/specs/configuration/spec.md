## ADDED Requirements

### Requirement: Настройки контура «Счёт»
Нормативный уровень: MUST. Секция `[contours.account]` ДОЛЖНА принимать ключи:

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `telegram_bot_env` | имя переменной окружения | `TELEGRAM_ACCOUNT_BOT_TOKEN` | токен бота контура |
| `telegram_channel_env` | имя переменной окружения | `TELEGRAM_ACCOUNT_CHANNEL_ID` | id канала «Счёт» |
| `reconcile_every_min` | целое от 1 до 60 | `1` | как часто сверять счёт |
| `orders` | логический | `false` | заявки на счёте; до Р2 допустимо только `false` |

`orders = true` ДОЛЖНО завершать загрузку `ConfigError` с подсказкой «заявки на счёте включаются на Р2 (enable-live-trading)». Незнакомый ключ, неверный тип или значение вне диапазона ДОЛЖНЫ завершать загрузку `ConfigError` с указанием файла и ключа; `bindings` в секции отклоняется, как у песочницы. Токен брокера и номер счёта ДОЛЖНЫ читаться только из `TINKOFF_TRADE_TOKEN` и `TINKOFF_ACCOUNT_ID` и НЕ ДОЛЖНЫ задаваться в `robot.toml`. Вшитый `default.toml` ДОЛЖЕН содержать секцию со значениями по умолчанию и комментариями.

#### Scenario: Значения по умолчанию
- **WHEN** секции `[contours.account]` в `robot.toml` нет
- **THEN** контур «Счёт» берёт бота из `TELEGRAM_ACCOUNT_BOT_TOKEN`, канал из `TELEGRAM_ACCOUNT_CHANNEL_ID`, сверяет счёт раз в минуту и работает только на чтение

#### Scenario: Заявки до Р2
- **WHEN** `robot.toml` содержит `[contours.account] orders = true`
- **THEN** загрузка завершается `ConfigError` с подсказкой про Р2

#### Scenario: Номер счёта в конфиге
- **WHEN** `robot.toml` содержит `[contours.account] account_id = "…"`
- **THEN** загрузка завершается `ConfigError`: незнакомый ключ, номер задаётся только в `TINKOFF_ACCOUNT_ID`
