## ADDED Requirements

### Requirement: Настройки контура эмуляции
Нормативный уровень: MUST. Секция `[contours.emulation]` ДОЛЖНА принимать ключ `telegram_channel_env` — имя переменной окружения с id канала Telegram контура (по умолчанию `TELEGRAM_CHANNEL_ID`, имя — латинские заглавные буквы, цифры и `_`). Значение id ДОЛЖНО читаться только из окружения; в `robot.toml` id канала НЕ ДОЛЖЕН задаваться. Если переменная не задана, контур ДОЛЖЕН работать без Telegram и предупредить в консоли. Ключ `[contours] market_feed` ДОЛЖЕН отклоняться `ConfigError` с подсказкой удалить строку; `[contours] feed_poll_secs` сохраняется. Незнакомые ключи `[contours.emulation]` ДОЛЖНЫ отклоняться `ConfigError` с указанием файла и ключа.

#### Scenario: Канал по умолчанию
- **WHEN** секции `[contours.emulation]` нет
- **THEN** контур отправляет в канал из `TELEGRAM_CHANNEL_ID`

#### Scenario: Свой канал контура
- **WHEN** `[contours.emulation] telegram_channel_env = "TELEGRAM_EMULATION_CHANNEL_ID"` и переменная задана в `.env`
- **THEN** контур отправляет в этот канал, значение id не попадает в журнал

#### Scenario: Старый переключатель
- **WHEN** `robot.toml` содержит `[contours] market_feed = false`
- **THEN** загрузка завершается `ConfigError`: ключ удалён, контур всегда читает ленту ядра

#### Scenario: Недопустимое имя переменной
- **WHEN** `telegram_channel_env = "telegram-id"`
- **THEN** загрузка завершается `ConfigError` с указанием ключа
