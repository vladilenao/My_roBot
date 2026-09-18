# Задачи: trade-artifact-refactor

## 1. Имена пользовательских артефактов

- [x] 1.1 Сменить дефолты в `src/config.py` (`_DEFAULTS`) и вшитых значениях: `journal_file` → `trade_event.csv`, `positions_file` → `trade_summary.csv`, `audit_file` → `trade_decision_trace.log`
- [x] 1.2 Обновить дефолты и валидацию в `src/config_loader.py` (ключи `_CONFIG` и таблицы default) и `src/config.py` (загрузка журнала: `journal_file`, `positions_file`, `audit_file`)
- [x] 1.3 Убедиться, что `CsvExporter`/`AuditExporter` в `src/trade_journal/export.py` читают новые имена из конфигурации (журнал/карточки/аудит) и НЕ создают legacy-копий старого формата
- [x] 1.4 Обновить интеграционные тесты экспорта (`tests/integration/test_trade_journal_export.py`) на новые имена `trade_event.csv`/`trade_summary.csv`: переименование при первом экспорте без legacy-копий

## 2. Запись следов решений в боевой цикл

- [x] 2.1 Подключить `CalculationTrace`/`calculation_trace()` к точкам, где расчёт уже строится (риск, профили, ATR/MA только в торговых решениях, basic_levels, apply_fill, резервирование), чтобы их следы записывались в `calculations` через `CalculationTraceRepository.record()` или транзакционный вариант `record_in_transaction()`
- [x] 2.2 Добавить вызовы `record()` в точки недостающих следов: наблюдаемая заглушка slippage guard, лимит экспозиции, решение о доборе, перенос стопа, trailing stop — каждый след с входными величинами, шагами и причиной
- [x] 2.3 Проверить, что данные экспортируются `AuditExporter` в `trade_decision_trace.log` из одного read-only снимка с идемпотентностью по `audit_exported_revision` и атомарностью замены
- [x] 2.4 Добавить/обновить тесты аудита (`tests/unit/trade_management/test_audit.py` и `tests/integration/...`) на запись следов решений и отсутствие legacy-файлов

## 3. Проверка и документация

- [x] 3.1 Прогнать `pytest` и `ruff` по всем изменённым модулям
- [x] 3.2 Проверить, что `trade_decision_trace.log` начинает наполняться в живом цикле (после исполнения события), а старые `trade_journal*.csv`/`trade_audit.log` больше не создаются
- [x] 3.3 Синхронизировать спеки после применения и убедиться, что валидация `trade-event\`/`trade-summary`/`trade-decision-trace` проходит
