## 1. Настройки

- [ ] 1.1 `portfolio_pct` в `[contours.emulation]`, `[contours.sandbox]`, `[contours.account]`: тип, диапазон 0–100, `None` по умолчанию; согласованность с `min_trade_risk_pct`; константы в `src/config.py`; комментарий в `default.toml`.

## 2. Торговая часть

- [ ] 2.1 `ContourSettings.portfolio_pct` → `_build_runtime` → `_risk_limits(pct)` и `TradeManager(portfolio_pct=…)`; пусто — общий процент.
- [ ] 2.2 `run.py`: процент эмуляции, песочницы и «Счёта» в их `ContourSettings`; история — без него.

## 3. Тесты и прогон

- [ ] 3.1 Юнит-тесты: загрузка ключа, диапазон, `bool`, согласованность с `min_trade_risk_pct`; бюджет риска менеджера от процента контура и от общего без ключа.
- [ ] 3.2 Полный `pytest` и `ruff check src tests run.py`.

## 4. Проверка владельцем

- [ ] 4.1 `[contours.account] portfolio_pct = …`, перезапуск «Счёта»: сигнал Si входит хотя бы на 1 лот (или отказ `risk-budget` с бюджетом от нового процента в логе контура); у песочницы и эмуляции бюджет прежний.
