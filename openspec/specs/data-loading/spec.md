# Загрузка данных

## Purpose

Загружает исторические OHLCV-данные свечей из Invest API Т-Банка для заданного инструмента, таймфрейма и диапазона дат, возвращая их в виде структурированного pandas DataFrame, готового к техническому анализу.

## Requirements

### Requirement: Основная функция загрузки свечей
- Система ДОЛЖНА предоставлять функцию `load_candles(ticker, instrument_type, timeframe, start_date, end_date, token)`.

#### Scenario: Успешная загрузка свечей
- **WHEN** свечи доступны для запрошенного диапазона
- **THEN** возвращается DataFrame со столбцами `['datetime', 'open', 'high', 'low', 'close', 'volume']`, где все значения цен — float, а `datetime` — без часового пояса

### Requirement: Валидация таймфрейма
- Если `timeframe` не является ключом словаря `TIMEFRAMES`, система ДОЛЖНА выбросить `ValueError`.

#### Scenario: Недопустимый таймфрейм
- **WHEN** `load_candles` вызывается с `timeframe="2h"` (отсутствует в `TIMEFRAMES`)
- **THEN** выбрасывается `ValueError` с сообщением "Неподдерживаемый таймфрейм"

### Requirement: Диапазон дат по умолчанию
- Если `start_date` равен `None`, система ДОЛЖНА использовать значение по умолчанию — 30 дней от текущего момента (`now() - timedelta(days=30)`).
- Если `end_date` равен `None`, система ДОЛЖНА использовать текущий момент (`now()`).

#### Scenario: Диапазон дат по умолчанию загружает последние 30 дней
- **WHEN** `load_candles` вызывается с `start_date=None` и `end_date=None`
- **THEN** загружаются свечи от 30 дней назад до текущего момента для указанного тикера и таймфрейма

### Requirement: Парсинг строковых дат
- Если `start_date` или `end_date` переданы в виде строк, система ДОЛЖНА разобрать их с использованием формата `%Y-%m-%d`.

#### Scenario: Строковые даты преобразуются в datetime
- **WHEN** `load_candles` вызывается с `start_date="2025-01-01"` и `end_date="2025-02-01"`
- **THEN** обе строки преобразуются в объекты `datetime` и используются для диапазона загрузки свечей

### Requirement: Валидация порядка дат
- Если `start_date >= end_date`, система ДОЛЖНА выбросить `ValueError`.

#### Scenario: Дата начала позже даты окончания
- **WHEN** `load_candles` вызывается с `start_date` больше или равным `end_date`
- **THEN** выбрасывается `ValueError` с сообщением "Дата начала должна быть меньше даты окончания"

### Requirement: Определение UID инструмента
- Система ДОЛЖНА определить UID инструмента вызовом `find_working_instrument(client, ticker, instrument_type)`.

#### Scenario: UID успешно определён
- **WHEN** тикер существует и доступен через API
- **THEN** вызывается `find_working_instrument` и возвращённый UID используется для запроса свечей

### Requirement: Конвертация формата цен
- Система ДОЛЖНА конвертировать цену каждой свечи из формата Т-Банка `units + nano/1e9` в стандартные float-значения OHLCV.

#### Scenario: Конвертация units+nano в float
- **WHEN** свеча от API имеет `open.units=3` и `open.nano=500000000`
- **THEN** результирующее значение `open` равно `3.5`

### Requirement: Структура возвращаемого DataFrame
- Система ДОЛЖНА вернуть кортеж `(DataFrame, instrument_id)`, где DataFrame содержит столбцы `['datetime', 'open', 'high', 'low', 'close', 'volume']`.
- Столбец `datetime` ДОЛЖЕН иметь удалённую информацию о часовом поясе (`tz_localize(None)`).

#### Scenario: Формат возвращаемых данных
- **WHEN** `load_candles` успешно загружает свечи
- **THEN** возвращается кортеж `(DataFrame, instrument_id)`, где DataFrame содержит ровно 6 столбцов: `datetime`, `open`, `high`, `low`, `close`, `volume`

### Requirement: Обработка пустого результата
- Если свечи не возвращены, система ДОЛЖНА вернуть пустой DataFrame вместе с instrument_id.

#### Scenario: Свечи не возвращены
- **WHEN** API возвращает ноль свечей для заданного диапазона
- **THEN** возвращается пустой `pd.DataFrame` вместе с `instrument_id`
