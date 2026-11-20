## Why

17.11.2026, проверка Р2 (`enable-live-trading`): контур «Счёт» получил три сигнала по Si и все отклонил с причиной `risk-budget`. Бюджет риска — `[trading.risk_limits] portfolio_pct = 2` от капитала счёта: 198 ₽ при 9 900 ₽, а риск одного лота Si — 234–528 ₽. `portfolio_pct` один на весь робот, поэтому поднять риск только для «Счёта» нельзя: изменится и эмуляция, и песочница. Владелец решила: у каждого контура должен быть свой размер риска.

## What Changes

- **Ключ `portfolio_pct`** в `[contours.emulation]`, `[contours.sandbox]` и `[contours.account]`: число от 0 до 100, процент капитала контура — бюджет риска всех его открытых остатков и ожидающих входов. Нет ключа — действует общий `[trading.risk_limits] portfolio_pct`.
- **Торговая часть контура** берёт бюджет риска из своей секции: тот же расчёт `min(balance, equity) × portfolio_pct / 100`, те же отказы `risk-budget`, другой процент.
- **Проверка согласованности:** `min_trade_risk_pct` не может превышать `portfolio_pct` контура — как сейчас для общего значения.
- Прогоны истории не меняются: у них общий `portfolio_pct`.

## Capabilities

### New Capabilities

Нет.

### Modified Capabilities

- `configuration`: свой риск портфеля у контура эмуляции, песочницы и «Счёта».

## Impact

- **Код:** `src/config_loader.py`, `src/config.py`, `src/assembly/contour.py` (`ContourSettings.portfolio_pct`, `_build_runtime`, `_risk_limits`), `run.py` (передача процента контуров).
- **API, журнал, события:** без изменений; бюджет риска виден в отказах и трассе расчёта, как сейчас.
- **Ветка:** `feature/contour-risk` от `feature/account-contour`.
