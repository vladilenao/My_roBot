## MODIFIED Requirements

### Requirement: Агрегация сигналов за скользящее окно
Агрегация сигналов индикаторов ДОЛЖНА выполняться общим модулем `src/strategies/signals.py`: скользящее окно размером `strategy_window` из `StrategyConfig` суммирует значения сигнальных колонок по всей истории. Сигнальные колонки определяются динамически из `StrategyConfig.signal_columns`, а не хардкодятся.

#### Scenario: Окно из 5 свечей
- **WHEN** DataFrame содержит ≥5 строк и `strategy_window=5`
- **THEN** каждая сумма — арифметическая сумма последних 5 значений соответствующей сигнальной колонки

#### Scenario: Окно больше данных
- **WHEN** DataFrame содержит 7 строк, окно стратегии равно 10
- **THEN** окно уменьшается до 7, суммы по всем строкам

#### Scenario: Динамические сигнальные колонки
- **WHEN** `StrategyConfig` содержит только `MacdIndicator` и `RsiIndicator`
- **THEN** агрегация выполняется только по `macd_signal` и `rsi_signal`, без `stoch_signal`
