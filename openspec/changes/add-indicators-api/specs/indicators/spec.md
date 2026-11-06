## ADDED Requirements

### Requirement: Описание отрисовки индикатора
Нормативный уровень: MUST. Каждый индикатор стратегий (подкласс `Indicator` в `src/strategies/indicators/`) ДОЛЖЕН отдавать описание для графика — свойство `chart`:

- `key` — короткий ключ вида индикатора: `ma_cloud`, `bbands`, `rsi`, `macd`, `stochastic`;
- `title` — подпись с параметрами для легенды, например `MACD 12/26/9`;
- `params` — параметры экземпляра, которые меняют значения;
- `pane` — `price` (на свечах) или `separate` (своя панель под свечами);
- `levels` — горизонтальные уровни панели: RSI — 30, 50, 70; стохастик — 20, 80; MACD — 0; у остальных — пусто;
- `lines` — линии по порядку отрисовки: `name` (роль линии: `macd`, `signal`, `histogram`, `rsi`, `k`, `d`, `fast`, `slow`, `upper`, `middle`, `lower`), `kind` (`line` или `histogram`), `column` — колонка результата `compute()`; у облака MA — заливка между `fast` и `slow`.

Колонка каждой линии ДОЛЖНА присутствовать в результате `compute()` этого экземпляра. Цвета описание НЕ ДОЛЖНО задавать.

#### Scenario: Описание MACD
- **WHEN** у `MacdIndicator(fast=12, slow=26, signal=9)` запрашивается `chart`
- **THEN** `pane = separate`, `levels = [0]`, линии `macd`, `signal` (`line`) и `histogram` (`histogram`) с колонками `macd_12_26_9`, `macds_12_26_9`, `macdh_12_26_9`

#### Scenario: Колонки описания есть в результате
- **GIVEN** любой индикатор из конфигураций зарегистрированных стратегий и кадр OHLCV
- **WHEN** вызывается `compute()`
- **THEN** все колонки `chart.lines` есть в результате

#### Scenario: Облако MA на свечах
- **WHEN** у `MaCloudIndicator(fast_period=10, slow_period=40)` запрашивается `chart`
- **THEN** `pane = price`, линии `fast` и `slow` с колонками `sma_fast` и `sma_slow`, заливка между ними
