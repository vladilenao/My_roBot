## 1. Расширение контракта Decision

- [x] **1.1** Добавить поля в `src/strategies/contracts.py` — `action: str | None = None`, `exit_reason: str | None = None`, `exit_contracts: int | None = None`; проверить что существующие unit-тесты проходят
- [x] **1.2** Убедиться что snapshot-тесты для `macd_rsi_stoch`, `flat_triangle`, `harmonic_abcd` продолжают проходить (новые поля `None`)

## 2. Индикатор MA (облако)

- [x] **2.1** Создать пакет `src/strategies/indicators/ma/` — `__init__.py`, `signalEnum.py` (`MaCloudSignalEnum`: MA_CROSS_UP/M_A_CROSS_DOWN/NO_SIGNAL), `indicator.py` (`MaCloudIndicator`: SMA 10/40, кроссовер, warmup=40, signal_column=`ma_cloud_signal`)
- [x] **2.2** Unit-тесты для `MaCloudIndicator` — кроссовер вверх, вниз, нет кроссовера, прогрев

## 3. Индикатор MACD Zero Cross

- [x] **3.1** Создать пакет `src/strategies/indicators/macd_zero_cross/` — `__init__.py`, `signalEnum.py` (`MacdZeroCrossSignalEnum`: MACD_CROSS_ABOVE_ZERO/M_A_CROSS_BELOW_ZERO/NO_SIGNAL), `indicator.py` (`MacdZeroCrossIndicator`: MACD 12/26/9, пересечение нуля сигнальной линией, warmup=35, signal_column=`macd_zero_signal`)
- [x] **3.2** Unit-тесты для `MacdZeroCrossIndicator` — кроссовер вверх, вниз, нет кроссовера, прогрев

## 4. Стратегия MA Cloud RSI MACD

- [x] **4.1** Создать файл `src/strategies/ma_cloud_rsi_macd_strategy.py` — `DEFAULT_CONFIG`, класс `MaCloudRsiMacdStrategy` с `@register`, `__init__`, `compute()`, `required_history()=41`
- [x] **4.2** Реализовать логику «третий сигнал» в `decide()` — отслеживание индикаторов за 6 свечей, вход при третьем подтверждении, сброс по истечении окна
- [x] **4.3** Реализовать логику добора (scale-in) — ретест облака, объём ≤50% позиции
- [x] **4.4** Реализовать логику выхода (exit) — ступенчатый: 1 контракт (только MA40),>1 контракта (MA10/облако/MA40), заполнение `exit_reason` и `exit_contracts`
- [x] **4.5** Unit-тесты стратегии — вход Long/Short, HOLD, сброс окна, добор, выход (1/>1/полный), независимость позиций

## 5. Реестр и конфигурация

- [x] **5.1** Добавить `"ma_cloud_rsi_macd"` в `StrategyName` Literal в `src/strategies/names.py`
- [x] **5.2** Добавить `DEFAULT_CONFIG` маппинг в `_strategy_map()` в `run.py`
- [x] **5.3** Добавить `ma_cloud_rsi_macd` в `share_strategies` и `future_strategies` в `default.toml`

## 6. Snapshot-тесты

- [x] **6.1** Подготовить CSV с OHLCV данными (≥3 кроссовера MA, 3 RSI, 3 MACD) в `tests/snapshot/data/<INSTRUMENT>_<TIMEFRAME>/candles.csv`
- [x] **6.2** Сгенерировать `ma_cloud_rsi_macd_expected_signals.csv` через `tools/download_snapshot_data.py`

## 7. Проверка и финализация

- [x] **7.1** `ruff check` — 0 ошибок, `ruff format` — 0 изменений
- [x] **7.2** `python -m pytest` — все тесты проходят
