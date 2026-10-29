## ADDED Requirements

### Requirement: Настройки контура песочницы
Нормативный уровень: MUST. Секция `[contours.sandbox]` ДОЛЖНА принимать ключи:

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `telegram_bot_env` | имя переменной окружения | `TELEGRAM_ACCOUNT_BOT_TOKEN` | токен бота контура |
| `telegram_channel_env` | имя переменной окружения | `TELEGRAM_SANDBOX_CHANNEL_ID` | id канала контура |
| `reconcile_every_min` | целое от 1 до 60 | `1` | как часто сверять счёт |

Имена переменных ДОЛЖНЫ состоять из латинских заглавных букв, цифр и `_`. Значения токена и id канала ДОЛЖНЫ читаться только из окружения и НЕ ДОЛЖНЫ задаваться в `robot.toml`. Незнакомый ключ, неверный тип или значение вне диапазона ДОЛЖНЫ завершать загрузку `ConfigError` с указанием файла и ключа. В частности, `bindings` в секции отклоняется с подсказкой: состав задаётся командой `PUT /api/v1/sandbox/target` или `POST /api/v1/sandbox/bindings`. Вшитый `default.toml` ДОЛЖЕН содержать секцию со значениями по умолчанию и комментариями.

Токен бота из `telegram_bot_env` ДОЛЖЕН передаваться в маскирование лога робота и контура как секрет.

#### Scenario: Значения по умолчанию
- **WHEN** секции `[contours.sandbox]` в `robot.toml` нет
- **THEN** контур берёт бота из `TELEGRAM_ACCOUNT_BOT_TOKEN`, канал из `TELEGRAM_SANDBOX_CHANNEL_ID` и сверяет счёт раз в минуту

#### Scenario: Состав в конфиге
- **WHEN** `robot.toml` содержит `[contours.sandbox] bindings = ["future-ng-macd"]`
- **THEN** загрузка завершается `ConfigError` с подсказкой задавать состав командой API

#### Scenario: Частота вне диапазона
- **WHEN** `reconcile_every_min = 0`
- **THEN** загрузка завершается `ConfigError` с указанием ключа
