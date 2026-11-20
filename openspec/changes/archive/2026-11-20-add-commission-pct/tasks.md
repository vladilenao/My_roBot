## 1. Настройки

- [x] 1.1 `commission_pct` в `[trading.risk_limits]` и `[contours.emulation|sandbox|account]`: число 0–1, `None` по умолчанию в коде; `default.toml` — общий 0.025, песочница 0.05; `config.py` — проценты контуров.

## 2. Расчёт

- [x] 2.1 `build_management_market(commission_pct=…)`: рубли за контракт от цены сигнала или закрытия бара, округление вверх до копейки; без цены или карточки — `commission`.
- [x] 2.2 `TradeManager(commission_pct=…)` во всех вызовах `build_management_market`; `_build_runtime` и `ContourSettings.commission_pct`; `run.py` — процент эмуляции, песочницы и «Счёта», история — общий.

## 3. Тесты и прогон

- [x] 3.1 Юнит-тесты: загрузка ключа, диапазон, `bool`; комиссия плана BR, Si, акции; без процента — рубли; приоритет секции контура над общим; комиссия исполнения эмуляции по снимку.
- [x] 3.2 Полный `pytest` и `ruff check src tests run.py`.

  Сделано 19.11: `_commission_pct` и ключи в `config_loader.py`, `config.py`, `default.toml` (общий 0.025, песочница 0.05); `_commission_per_contract` в `pipeline.py`; `TradeManager(commission_pct=…)`; `_build_runtime`/`ContourSettings.commission_pct`; `run.py` `_contour_commission_pct`. Тесты: `test_market_view.py` (+3), `test_contour_config.py` (+6), `test_run.py` (обновлён). Полный `pytest`: 4209 passed и 1 тест композиции (`test_run.py`), обновлённый под новый аргумент, — после правки `tests/unit` 4014 passed; `ruff check` чистый.

## 4. Проверка владельцем

- [x] 4.1 Перезапуск робота: в трассе расчёта нового плана «Счёта» комиссия за контракт ≈ 0,025% его стоимости, у песочницы ≈ 0,05%; комиссия брокера после исполнения по-прежнему заменяет оценку.
  Частично 19.11 после перезапуска в 22:36: песочница — Si `levels_rr.manage` 19:38 UTC, entry_cost = exit_cost = 41.23 (82 455 × 0,05%, было 1.5); «Счёт» берёт общий 0.025 (своего ключа нет), по конфигу BR 63.5 → 13.10, Si 82 455 → 20.62. Пройдено 20.11: «Счёт» — план BR 06:30 UTC entry_cost = exit_cost = 19.13 (92.72 × 825.143 × 0,025%), песочница по тому же сигналу 38.37 (0,05%); GOLD «Счёта» 73.63. Сделка BR ×2: оценка 38.26 за исполнение заменена комиссией брокера 37.98 (вход) и 37.86 (стоп) в cost_adjustments, fee_source=broker; в сводке комиссия −75.84, «брокерские суммы».
