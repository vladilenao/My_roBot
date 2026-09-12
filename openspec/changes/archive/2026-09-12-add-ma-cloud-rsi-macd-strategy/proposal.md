## Why

Расширение набора стратегий: добавляется MA Cloud RSI MACD — двусторонняя трендовая стратегия, использующая пересечение скользящих средних (облако MA 10/40), RSI (уровень 50) и MACD (нулевая линия) для определения направления входа. Стратегия поддерживает добор при ретесте облака и ступенчатый выход в зависимости от размера позиции — это единственная стратегия в проекте с такой логикой.

## What Changes

- Добавлен индикатор MA (Moving Average) в `src/strategies/indicators/ma/` с кроссовером MA 10/40.
- Добавлен индикатор MACD Zero Cross в `src/strategies/indicators/macd_zero_cross/` с определением пересечения нулевой линии сигнальной линией MACD.
- Добавлен файл стратегии `src/strategies/ma_cloud_rsi_macd_strategy.py` с логикой «третий сигнал», добора и ступенчатого выхода.
- Стратегия регистрируется в реестре под именем `ma_cloud_rsi_macd`.
- Добавлен `StrategyName` в `src/strategies/names.py`.
- Добавлен `DEFAULT_CONFIG` в `src/strategies/ma_cloud_rsi_macd_strategy.py` для маппинга в `run.py`.
- Стратегия добавляется в `share_strategies` / `future_strategies` по умолчанию в `default.toml`.
- Добавляются snapshot-тесты для верификации алгоритма.
- В `Decision` добавлены поля `action` и `exit_reason` для дифференциации типов сигналов (вход, добор, выход).

## Capabilities

### New Capabilities
- `strategy/ma-cloud-rsi-macd`: двусторонняя трендовая стратегия на базе MA 10/40 (облако), RSI 50 и MACD 0 с логикой добора и ступенчатого выхода.

### Modified Capabilities
- `strategy-contract`: расширение контракта `Decision` полями `action` и `exit_reason` для поддержки типов сигналов, выхода и добора.
