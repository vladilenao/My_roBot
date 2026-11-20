## Context

Бюджет риска портфеля считает `TradeManager` по `PortfolioBudgetPolicy(portfolio_pct)` и `RiskLimits(portfolio=…)`; оба собираются в `src/assembly/contour.py` (`_build_runtime`, `_risk_limits`) из глобального `RISK_LIMITS["portfolio_pct"]`. `ContourSettings` уже несёт всё, чем контуры различаются; `run.py` строит его для эмуляции, контуров на брокере (через `_broker_trading_factory`) и прогонов истории.

## Decisions

### 1. Ключ в секции контура, по умолчанию — общий

`[contours.<вид>] portfolio_pct` для `emulation`, `sandbox`, `account`. Значение `None` (ключа нет) — `[trading.risk_limits] portfolio_pct`. Плоские имена `contours_<вид>_portfolio_pct`, константы `CONTOUR_<ВИД>_PORTFOLIO_PCT` (по умолчанию `None`).

*Альтернатива:* целая секция `[contours.<вид>.risk_limits]` со всеми лимитами. Отвергнута: владелец просила только размер риска; остальные лимиты (`max_qty`, издержки, отклонения) — свойства рынка и робота, а не контура.

### 2. `ContourSettings.portfolio_pct`

Новое поле `portfolio_pct: float | None = None` передаётся в `_build_runtime` и оттуда в `_risk_limits(pct)` и `TradeManager(portfolio_pct=…)`. Пустое — прежнее поведение. История не передаёт поле.

### 3. Проверка

Загрузка настроек: `portfolio_pct` контура — конечное число от 0 до 100; `bool` и строки — `ConfigError` с ключом. Если `min_trade_risk_pct` больше `portfolio_pct` контура — `ConfigError`, как у общего значения.

## Risks / Trade-offs

- **[Разный риск — разные объёмы в сравнении трёх контуров]** → ожидаемо: расхождение `lots` в `GET /api/v1/account/comparison` объясняется процентом.
- **[Большой процент на настоящем счёте]** → предохранитель «дневной убыток» (`max_daily_loss_pct`) по-прежнему останавливает входы.

## Migration Plan

Без ключей поведение прежнее. Изменение вступает в силу при перезапуске процесса контура.
