## ADDED Requirements

### Requirement: Файл карточек позиций
Система ДОЛЖНА читать опциональный ключ `positions_file` из секции `[trading]` файла `robot.toml` (или вшитого дефолта `default.toml`) — имя CSV-файла карточек позиций. При отсутствии ключа имя ДОЛЖНО выводиться из `journal_file`: к имени файла ленты добавляется суффикс `_positions` перед расширением (например, `trade_journal.csv` → `trade_journal_positions.csv`). Значение ключа ДОЛЖНО быть непустой строкой; при недопустимом значении загрузка ДОЛЖНА завершаться `ConfigError` с указанием файла и ключа.

#### Scenario: Ключ задан явно
- **WHEN** в секции `[trading]` задан `positions_file = "positions.csv"`
- **THEN** карточки позиций ведутся в файле `positions.csv`

#### Scenario: Ключ не задан
- **WHEN** ключ `positions_file` в секции `[trading]` отсутствует, а `journal_file = "trade_journal.csv"`
- **THEN** карточки позиций ведутся в выводном файле `trade_journal_positions.csv`

#### Scenario: Недопустимое значение ключа
- **WHEN** ключ `positions_file` задан пустой строкой
- **THEN** загрузка конфигурации завершается `ConfigError` с указанием файла и ключа