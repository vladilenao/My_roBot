# Уведомления

## Purpose

Доставляет сообщения с торговыми сигналами во внешний канал. Основной транспорт — Telegram Bot API через HTTP POST, с запасным выводом в stdout, когда Telegram не настроен.

## Requirements

### Requirement: Основная функция отправки
- Система ДОЛЖНА предоставлять функцию `send_signal(text)`, принимающую строку сообщения.

#### Scenario: Сообщение выводится в stdout
- **WHEN** `send_signal` вызывается с любым текстом
- **THEN** текст выводится в stdout независимо от статуса настройки Telegram

### Requirement: Проверка конфигурации Telegram
- Если `TELEGRAM_BOT_TOKEN` или `TELEGRAM_CHANNEL_ID` не заданы (None или пустые), система ДОЛЖНА вывести предупреждение в stdout и пропустить вызов Telegram API.

#### Scenario: Telegram не настроен
- **WHEN** `TELEGRAM_BOT_TOKEN` равен `None` или пуст
- **THEN** `send_signal` выводит предупреждение "Telegram не настроен" в stdout и не выполняет HTTP-запросов

### Requirement: Отправка через Telegram Bot API
- Когда Telegram настроен, система ДОЛЖНА отправлять HTTP POST на `https://api.telegram.org/bot{token}/sendMessage` с полями `chat_id` и `text`.
- HTTP-запрос ДОЛЖЕН иметь таймаут 10 секунд.

#### Scenario: Telegram настроен и отправка успешна
- **WHEN** `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHANNEL_ID` заданы И Telegram API возвращает HTTP 200
- **THEN** сообщение отправляется в настроенный канал И выводится в stdout

### Requirement: Обработка ошибок HTTP
- Если статус HTTP-ответа не 200, система ДОЛЖНА вывести текст ошибки ответа в stdout.

#### Scenario: Telegram API возвращает ошибку
- **WHEN** Telegram API возвращает код статуса, отличный от 200
- **THEN** текст ошибки ответа выводится в stdout

### Requirement: Обработка сетевых исключений
- Если во время HTTP-вызова возникает исключение, система ДОЛЖНА вывести сообщение об исключении в stdout без проброса выше.

#### Scenario: Сетевое исключение при отправке
- **WHEN** во время HTTP POST возникает исключение (например, таймаут, ошибка соединения)
- **THEN** сообщение об исключении выводится в stdout и исключение не пробрасывается вызывающему коду

### Requirement: Гарантированный вывод в stdout
- Текст сообщения ВСЕГДА ДОЛЖЕН выводиться в stdout независимо от того, успешно ли отправлен Telegram или операция пропущена.

#### Scenario: Вывод при настроенном Telegram
- **WHEN** Telegram настроен И отправка проходит успешно
- **THEN** сообщение выводится в stdout параллельно с отправкой в Telegram

#### Scenario: Вывод при отсутствии Telegram
- **WHEN** Telegram не настроен
- **THEN** сообщение выводится в stdout с предупреждением о отсутствии конфигурации
