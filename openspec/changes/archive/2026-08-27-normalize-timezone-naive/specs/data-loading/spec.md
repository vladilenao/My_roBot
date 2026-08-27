## MODIFIED Requirements

### Requirement: Валидация порядка дат
Если `start_date >= end_date`, выбрасывается `ValueError`. Перед сравнением обе границы ДОЛЖНЫ быть приведены к единому tz-aware UTC состоянию, чтобы сравнение naive- и aware-значений никогда не происходило.

#### Scenario: Неверный порядок
- **WHEN** `start_date >= end_date`
- **THEN** `ValueError` "Дата начала должна быть меньше даты окончания"

#### Scenario: Границы без часового пояса не падают
- **WHEN** `start_date` задан как naive (без пояса), а `end_date` получен как aware (с поясом) или наоборот
- **THEN** обе границы приводятся к единому aware-UTC перед сравнением, сравнение выполняется без ошибки типа

### Requirement: Удаление часового пояса
Данные ДОЛЖНЫ быть tz-naive: столбец `datetime` ДОЛЖЕН иметь `tz_localize(None)`. Внутри робота время naive по соглашению, а на исходящей границе к API `start_date`/`end_date` ДОЛЖНЫ приводиться к aware-UTC (клиент Tinkoff не принимает naive `from_`/`to`).

#### Scenario: Время без таймзоны
- **WHEN** DataFrame возвращён
- **THEN** `datetime` не содержит информации о часовом поясе

#### Scenario: Aware-границы к API
- **WHEN** `start_date` или `end_date` передаются в API
- **THEN** они приведены к tz-aware UTC, согласованно с требованием клиента Tinkoff, и не смешиваются с naive внутри робота
