# Конфигурация

## MODIFIED Requirements

### Requirement: Маппинг таймфреймов
- Система ДОЛЖНА предоставлять словарь `TIMEFRAMES`, отображающий строковые ключи (`1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`, `1w`, `1M`) в `CandleInterval`.

#### Scenario: Допустимый ключ
- **WHEN** потребитель ищет `"1h"` в `TIMEFRAMES`
- **THEN** возвращается `CandleInterval.CANDLE_INTERVAL_HOUR`

#### Scenario: Тридцатиминутный ключ
- **WHEN** потребитель ищет `"30m"` в `TIMEFRAMES`
- **THEN** возвращается `CandleInterval.CANDLE_INTERVAL_30_MIN`

#### Scenario: Четырёхчасовой ключ
- **WHEN** потребитель ищет `"4h"` в `TIMEFRAMES`
- **THEN** возвращается `CandleInterval.CANDLE_INTERVAL_4_HOUR`

#### Scenario: Недопустимый ключ
- **WHEN** потребитель ищет недопустимый ключ
- **THEN** выбрасывается `KeyError`

## ADDED Requirements

### Requirement: Секция конфигурации профиля triple_screen
Система ДОЛЖНА читать параметры профиля `triple_screen` из секции `[strategies.filter.triple_screen]` файла `robot.toml` (или вшитого дефолта `default.toml`): `multiplier` (целое, не менее 2, дефолт 5), `macd_fast`/`macd_slow`/`macd_signal` (целые, дефолты 12/26/9), `stoch_k`/`stoch_d`/`stoch_smooth_k` (целые, дефолты 14/3/3), `oversold`/`overbought` (целые, дефолты 20/80). Незнакомые ключи секции ДОЛЖНЫ отклоняться с `ConfigError` и указанием файла и ключа. При отсутствии секции применяются дефолты.

#### Scenario: Дефолтные параметры
- **WHEN** секция `[strategies.filter.triple_screen]` отсутствует в конфигурации
- **THEN** применяются дефолты: множитель 5, MACD (12, 26, 9), Stochastic (14, 3, 3), пороги 20/80

#### Scenario: Переопределение параметров
- **WHEN** секция задаёт `multiplier = 3` и `oversold = 25`
- **THEN** эти значения используются в расчётах фильтра

#### Scenario: Незнакомый ключ секции
- **WHEN** секция содержит незнакомый ключ (например, `window`)
- **THEN** загрузка конфигурации завершается `ConfigError` с указанием файла и ключа

### Requirement: Валидация иерархии таймфреймов triple_screen
При загрузке конфигурации система ДОЛЖНА вычислять иерархию (`TF_int`, `TF_htf`) для каждой привязки с профилем `triple_screen` на основе лестницы `TIMEFRAMES` и заданного множителя. Если для рабочего таймфрейма привязки не существует допустимых `TF_int` или `TF_htf` (шаг множителя выводит за пределы лестницы) — загрузка ДОЛЖНА завершаться `ConfigError` с указанием привязки.

#### Scenario: Допустимая иерархия
- **WHEN** рабочее ТФ привязки — `5m` и профиль `triple_screen`
- **THEN** валидация проходит: `TF_int = 30m`, `TF_htf = 4h`

#### Scenario: Иерархия за пределами лестницы
- **WHEN** рабочее ТФ привязки — `1M` и профиль `triple_screen`
- **THEN** загрузка конфигурации завершается `ConfigError` с указанием привязки