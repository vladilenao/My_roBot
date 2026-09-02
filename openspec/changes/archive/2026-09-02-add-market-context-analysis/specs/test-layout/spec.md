## ADDED Requirements

### Requirement: Хранение analysis-эталонов
Analysis-эталоны (тренд, уровни S/R) ДОЛЖНЫ храниться в той же папке кейса `tests/snapshot/data/<КЕЙС>/`, что и фикстура `candles.csv` и стратегические эталоны. Они ДОЛЖНЫ использовать отдельный суффикс `_expected_context.csv` (а не `_expected_signals.csv`), чтобы не конфликтовать с паттерном стратегических эталонов и не быть подхваченными раннером стратегий. Свечи кейса остаются общими для стратегий и analysis-слоя.

#### Scenario: Несколько типов эталонов в одном кейсе
- **WHEN** в кейсе `NG_1h/` лежат `macd_rsi_stoch_expected_signals.csv`, `trend_expected_context.csv` и `sr_levels_expected_context.csv`
- **THEN** каждый тип эталона обрабатывается своим раннером: сигналы — `test_strategies.py`, контекст — `test_analysis.py`; уровни данных в путях не смешиваются

#### Scenario: Новая analysis-продукция подхватывается автоматически
- **WHEN** появляется новый вид analysis-эталона (например, `filter_expected_context.csv`)
- **THEN** он обнаруживается параметризацией раннера `test_analysis.py` без правки кода теста
