# Контракт стратегий — расширение Decision

## Purpose

Расширение контракта `Decision` полями `action` и `exit_reason` для поддержки добора (scale-in) и ступенчатого выхода (exit) в стратегии MA Cloud RSI MACD. Существующие стратегии продолжают работать без изменений.

## Modified Requirements

### Requirement: Тип решения без форматирования (MODIFIED)

Метод `decide()` ДОЛЖЕН (MUST) возвращать решение в виде `SignalType` (`BUY`, `SELL`, `HOLD`) и цены; формирование человекочитаемых текстов в стратегии ЗАПРЕЩЕНО. Стратегия при вызове `decide()` НЕ ДОЛЖНА (MUST NOT) заполнять поля SL/TP/trend — они остаются `None`.

Модель `Decision` ДОЛЖНА (MUST) содержать новые поля с дефолтами `None` для обратной совместимости:
- `action: str | None` — тип действия: `"entry"`, `"scale_in"` или `None` (обратная совместимость для существующих стратегий).
- `exit_reason: str | None` — причина выхода: `"close_below_ma10"`, `"close_below_ma40"`, `"close_inside_cloud"` или `None`.
- `exit_contracts: int | None` — количество контрактов для частичного выхода; `None` — полный выход.

Новые поля ДОЛЖНЫ (MUST) иметь дефолт `None`; существующие стратегии, не заполняющие эти поля, продолжают работать.

#### Scenario: Обратная совместимость — существующая стратегия
- **WHEN** существующая стратегия (например, `MacdRsiStochStrategy`) возвращает `Decision` только с `signal_type` и `price`
- **THEN** поля `action`, `exit_reason`, `exit_contracts` имеют значение `None`, система работает без ошибок

#### Scenario: Новый тип решения — entry
- **WHEN** стратегия MA Cloud RSI MACD определяет вход в позицию
- **THEN** `Decision` содержит `action="entry"`, `exit_reason=None`, `exit_contracts=None`

#### Scenario: Новый тип решения — scale-in
- **WHEN** стратегия MA Cloud RSI MACD определяет добор в позицию
- **THEN** `Decision` содержит `action="scale_in"`, `exit_reason=None`, `exit_contracts=None`

#### Scenario: Новый тип решения — partial exit
- **WHEN** стратегия MA Cloud RSI MACD определяет частичный выход из позиции (>1 контракта)
- **THEN** `Decision` содержит `action=None`, `exit_reason="close_below_ma10"` или другое значение, `exit_contracts=1`

#### Scenario: Новый тип решения — full exit
- **WHEN** стратегия MA Cloud RSI MACD определяет полный выход из позиции
- **THEN** `Decision` содержит `exit_reason="close_below_ma40"`, `exit_contracts=None`

#### Scenario: Существующие поля SL/TP/trend не затронуты
- **WHEN** создаётся `Decision` с новыми полями
- **THEN** все существующие поля (`stop_loss`, `take_profit`, `trend_direction` и т.д.) остаются с дефолтами `None`
