# Проверка требований и сценариев

Итоговый прогон перед sync/archive: `.venv/bin/python -m pytest -q` — **1851 passed**;
`.venv/bin/python -m ruff check .` — те же 25 pre-existing ошибок только в
`tools/download_snapshot_data.py`, `tools/visualize_signals.py`, `openspec/_check_sync.py`.
Изменённый код (`src tests run.py tools/show_state.py`) — без lint-ошибок.
Strict validation delta прошла, `openspec validate --specs` — 35/35;
диагностическая `--specs --strict` — прежние 32 failures по MUST/SHALL WARNING.
Новые ошибки/предупреждения в целевой delta отсутствуют.

Sync/archive выполнен 06.10.2026: после финального прогона все 7 delta-спеков
(configuration, execution, notification, risk-management, snapshot-testing,
trade-journal, trade-management) синхронизированы в main specs — `openspec validate
--specs` 35/35, повторный `--strict` для change валиден; Purpose основных
risk/config specs согласованы с единым бюджетом `portfolio_pct`; change
`trade-economics-and-exit-geometry` заархивирован в
`openspec/changes/archive/2026-10-06-trade-economics-and-exit-geometry/`.
Старая delta предшественника повторно не накладывалась.

Матрица относится к эффективному объёму после подготовки корпуса акций (задача 8.1):
реальные исторические срезы GMKN/VTBR/TATN/PLZL 15m присутствуют в
`tests/snapshot/data/<TICKER>_15m/` с замороженными provenance-метаданными
(источник, диапазон, cutoff, sha256, шаг/лот/расходы, версия алгоритма).
Каждая строка ниже сопоставляет requirement и его scenario-группы с независимыми
unit-проверками и/или production lifecycle replay. Все unit-пути — `tests/unit/`,
replay — `tests/snapshot/test_management_lifecycle.py`, планы — `test_management_plans.py`.

## Configuration

| Requirement / scenario-группа | Проверка |
| --- | --- |
| Явная конфигурация управления сделками: разные привязки, capability, геометрия, расходы и min-risk | `test_config_loader.py`; `trade_management/test_signal_pipeline.py`, `test_geometry_v2.py` |
| Настройки хранения риска и аудита: конечность, пути, сохранённый профиль удалённой связки | `test_config_loader.py`; `trade_management/test_manager.py`; `trade_journal/test_schema_v10.py` |
| Единый процентный лимит и миграция старых настроек: default 2, явный процент/0, old keys включая пустые/0, NaN/bool | `test_config_loader.py`; `test_show_state.py`; `trade_management/test_portfolio_budget_v2.py` |
| Единицы расходов и экономические пороги: 10 контрактов C=40, default 2/1.5/0.25, выключение, invalid values | `test_config_loader.py`; `trade_management/test_economics_v2.py`, `test_portfolio_budget_v2.py` |
| Порог BE и формы лестницы: legacy pair до default merge, смешанные формы, retained remainder, min_be_r=0, system version | `test_config_loader.py`; `trade_management/test_exit_rules_v2.py` |

## Risk management

| Requirement / scenario-группа | Проверка |
| --- | --- |
| Размер позиции по итоговому риску: R+C, LONG/SHORT, max/requested Q, ГО/0 ГО, min-risk, confirmed/partial stop, unknown state | `portfolio/test_risk.py`; `trade_management/test_portfolio_budget_v2.py`, `test_pending_reservation_admission.py`; replay priority/margin |
| Резервы и конкуренция планов: priority/tie, последовательный остаток, partial/cancel, margin resize, race rollback | `bot/test_trading_bot.py`; `trade_management/test_pending_reservation_admission.py`, `test_portfolio_budget_v2.py`; replay priority/margin/partial |
| Контроль риска без блокировки выхода: запрет увеличений, отсутствие вытеснения, меньшая B, previously accepted fill | `portfolio/test_risk.py`; `trade_management/test_portfolio_budget_v2.py`; replay over-budget/close-open |
| Риск и прибыль по акции считаются от цены лота: V и симметричный gross | `trade_management/test_signal_pipeline.py`; `trade_journal/test_ruble_risk_metrics.py`; independent Decimal risk/fill tests |
| Экономический допуск после определения объёма: порядок, equality, exact 1.499, unknown total, zero expenses | `trade_management/test_portfolio_budget_v2.py`, `test_plan_economics.py`, `test_economics_v2.py` |
| Расходы учитываются в бюджете ровно один раз: одинаковые sizing/reserve, paid fee/realized без скидки, legacy future-only, выходы | `portfolio/test_risk.py`; `trade_management/test_portfolio_budget_v2.py`; replay full/partial/legacy/over-budget |

## Execution

| Requirement / scenario-группа | Проверка |
| --- | --- |
| Причинность свечной симуляции: stop priority, gap, next bar UTC, LIMIT touch/improvement/wait/deadline, intrabar/open, recovery, absolute D | `broker/test_limit_entries.py`; `trade_management/test_stop_boundary.py`; replay full/partial/add/close-open/gap-stop/pattern |
| Источник комиссии подтверждённого исполнения: broker/configured/unknown, known zero, incremental fills, frozen config, adjustment | `trade_journal/test_execution_costs.py`; `broker/test_limit_entries.py`; replay independent 30 ₽ / 45 ₽ |
| Проскальзывание наблюдается без повторного списания: confirmed reference, signed gap/favourable deviation, unknown reference | `trade_journal/test_execution_costs.py`; `broker/test_fill_bar_extremes.py`; replay gap-stop |
| Издержки сохраняют причинность и адресность: execution/command IDs, no gap fills, restart/dedup, fact above budget, notify-only | `test_run.py`; `trade_management/test_manager.py`; replay history-gap/over-budget + every scenario restart/repeat/rollback |

## Trade management

| Requirement / scenario-группа | Проверка |
| --- | --- |
| План и действия сопровождения: confirmed versus requested, frozen config/plan, rebase/absolute targets, completed facts, D0, versions | `trade_management/test_models.py`, `test_economics_v2.py`, `test_target_recalibration.py`, `test_portfolio_budget_v2.py`; replay all profiles/legacy |
| Геометрия защитного стопа: floor/cap, missing ATR, rounding <tick, conflict/tick, missing structure, numeric trace | `trade_management/test_geometry_v2.py`, `test_stop_geometry.py`, `test_profile_stop_geometry.py`; NG plan snapshots |
| Денежное представление плана: R/C, integer allocation, full/unknown reward, exact payoff, immutable quantity/version | `trade_management/test_plan_economics.py`, `test_economics_v2.py`; plan golden extended columns |
| Профиль levels_rr: support/resistance side, independent LONG/SHORT levels, cost-aware R targets, BE gate | `trade_management/test_levels_rr.py`, `test_geometry_v2.py`, `test_exit_rules_v2.py`; replay full/one/odd/partial/add |
| Профиль atr_trend: ATR, ladder, zero allocation threshold, retained remainder, monotonic extreme/ban-adds/recovery | `trade_management/test_atr_trend.py`, `test_exit_rules_v2.py`, `test_portfolio_budget_v2.py`; replay trailing |
| Профиль pattern_targets: available formation, target side, immutable midpoint/D, rejected economics, BE | `trade_management/test_pattern_targets.py`, `test_geometry_v2.py`, `test_plan_economics.py`, `test_exit_rules_v2.py`; replay pattern |
| Входной допуск возвращает причины недопуска: metadata/profile/structure, qty/duplicate/integrity, expiry, concrete budget/economics/unknown, success | `trade_management/test_signal_pipeline.py`, `test_manager.py`, `test_pending_reservation_admission.py`, `test_portfolio_budget_v2.py`; `bot/test_trading_bot.py` |
| R-цели покрывают издержки: unchanged structural stop, exact one compensation, directed rounding, unusable SHORT price | `trade_management/test_geometry_v2.py`; frozen plan/rebase tests; NG plan snapshots |
| Безубыток всей сделки после первой цели: TP1 + current 1.5D0, G/F/future costs, below-average BE, SHORT mirror, no retroactive protection | `trade_management/test_exit_rules_v2.py`; `broker/test_limit_entries.py`; replay full/partial/pattern/close-open |

## Trade journal

| Requirement / scenario-группа | Проверка |
| --- | --- |
| Правила заполнения атрибутов карточки: immutable idea versus fact, lifecycle, commission sign, RUB/RAW, ₽/R headers, provenance | `trade_journal/test_ruble_risk_metrics.py`, `test_holding_observations.py`; `tests/integration/test_trade_summary.py`; replay both CSVs |
| Карточка позиции: D0×V×Qmax, gap/add/partial, stable denominator, actual risk versus R+C, missing factors | `trade_management/test_portfolio_budget_v2.py`; `trade_journal/test_ruble_risk_metrics.py`; replay add/partial/one/odd |
| Доборы и частичные тейки: weighted average, actual residual, partially closed | `trade_journal/test_execution_costs.py`; `tests/integration/test_trade_summary.py`; replay partial/add/full |
| Расширенная карточка и финансовый результат: atomic balance/position/fills/reservations/targets/phase, adjustment, factors/RAW, rollback/dedup | `trade_journal/test_schema_v10.py`, `test_execution_costs.py`; journal integration tests; every replay with injected abort/delivery repeat |
| Отклонения позиции измеряются в рублях: LONG/SHORT clamp, held bars without fill, first/last boundaries, Qmax, unknown factors/coverage | `trade_journal/test_holding_observations.py`, `test_ruble_risk_metrics.py`; replay observations/measurements and history-gap |
| Миграция источников издержек и версии метрик: preserve v9 facts/account/pending, unknown old fees, legacy routing, mixed budget | `trade_journal/test_schema_v10.py`; `trade_management/test_economics_v2.py`, `test_portfolio_budget_v2.py`; replay real v9 legacy snapshot |

## Notification

| Requirement / scenario-группа | Проверка |
| --- | --- |
| Уведомление о принятой в работу сделке: selected/requested/constraint, plan-only state, gross/net/costs, zero/unknown/trailing | `notifier/test_templates.py`; `events/test_event_schemas.py`; `bot/test_trading_bot.py` |
| Экономический недопуск имеет русский смысл и код: numerical threshold, positive low payoff, concrete budget codes | `notifier/test_templates.py`; `events/test_event_schemas.py`; `trade_management/test_portfolio_budget_v2.py` |
| Общий риск и превышение видны пользователю: B/global pct/U/free/excess, no liquidation claim, unknown reason | `notifier/test_templates.py`; `test_run.py`; `trade_management/test_portfolio_budget_v2.py` observer/dedup/failure tests |
| Консольный отчёт согласован с портфельным бюджетом: new config, open+pending, confirmed/profit stop, legacy+v2, unknown/GO, read-only/concurrent writer | `test_show_state.py`: 39 independent cases |

## Snapshot testing

| Requirement / scenario-группа | Проверка |
| --- | --- |
| Воспроизводимый корпус экономики и lifecycle: source/cutoff/hash/factors/config/version, no live API, fixed selection | tracked NG_15m + LIFECYCLE_1m candles/metadata; `_load` hash check; explicit skipped stock slice |
| План и исполнение проверяются разными эталонами: integer plan fields, lifecycle facts, LONG/SHORT/small/partial/add/BE/trail/absolute/gap/legacy, independent costs/priority | 4 NG plan CSVs + 26 lifecycle golden variants; invariant assertions outside golden; independent priority/margin replay |
| Проверка разрывов не выдумывает наблюдения: gap stop, held bar, partial coverage, seven-day search limit | replay gap-stop/history-gap + production MarketDataCache/HistoricalRunSession fixture test, 2880 missed bars and bounded seven-day requests |

## Golden review и измерение

- Эталоны обновлены явно через `--update-snapshots`. Просмотрены разные целые
  аллокации Q=1/3/10/15, partial entry, цену close на open, gap benchmark,
  immutable plan, версии/полнота и строки русского CSV. Вне golden проверяются
  fees 30/45/3/9/0 ₽, gross/net, позиции/счёт/наблюдения/оба CSV после restart/repeat/abort.
- 46 snapshot-проверок прошли (31 lifecycle, включая cache/priority/margin/metrics,
  и 15 plan checks). Стратегический раннер и expected_signals.csv не изменены.
- LIFECYCLE_1m full BUY: 8 observations, 663 JSON-байта, replay 0.050 s.
  NG_15m real BUY: 55 observations, 4262 JSON-байта, replay 0.550 s.
  Это логический объём сериализованных наблюдений и замер текущего окружения,
  не обещание фиксированной скорости на другом компьютере.
- Whitelist в `.gitignore` включает только эти два replay-корпуса и связанные
  plan/lifecycle goldens; пользовательские data/HIST и стратегические срезы остаются
  вне этой правки. Raw NG candles не изменялись, hash закреплён в metadata.
