## ADDED Requirements

### Requirement: Торговая секция конфигурации
Система ДОЛЖНА предоставлять торговую секцию конфигурации (раздел `[trading]` в `robot.toml` или вшитые дефолты) со следующими ключами: `initial_deposit` (начальная сумма на счёте, руб, целое > 0), `max_risk_pct` (допустимый процент риска от текущего баланса, дробное в (0, 100]), `journal_file` (имя файла дневника сделок, по умолчанию `trade_journal.csv`), `clearing_times` (список моментов клиринга в формате `HH:MM`, по умолчанию `["14:05", "19:00"]`). Незнакомые ключи и недопустимые значения ДОЛЖНЫ отклоняться с `ConfigError` с указанием файла и ключа.

#### Scenario: Чтение торговых параметров
- **WHEN** модуль конфигурации читает торговую секцию
- **THEN** доступны константы `INITIAL_DEPOSIT`, `MAX_RISK_PCT`, `JOURNAL_FILE`, `CLEARING_TIMES` из `robot.toml` или вшитых дефолтов

#### Scenario: Недопустимый риск
- **WHEN** `max_risk_pct` задан значением 0 или 150
- **THEN** загрузка завершается `ConfigError`

#### Scenario: Недопустимый депозит
- **WHEN** `initial_deposit` не является положительным числом
- **THEN** загрузка завершается `ConfigError`

### Requirement: Импортируемые константы торговли
Все константы торговой секции ДОЛЖНЫ быть импортируемыми через `from src.config import INITIAL_DEPOSIT, MAX_RISK_PCT, JOURNAL_FILE, CLEARING_TIMES`.

#### Scenario: Прямой импорт
- **WHEN** выполняется `from src.config import MAX_RISK_PCT`
- **THEN** значение успешно импортируется