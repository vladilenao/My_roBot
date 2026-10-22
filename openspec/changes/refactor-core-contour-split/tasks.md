## 1. Порт и объекты

- [x] 1.1 `src/assembly/services.py`: протокол `MarketServices` (`timeline`, `data_cache`, `market_store`, `market_calendar`, `request_budget`, `pattern_sink`, `tracks_obligations`, `cluster_gate`, `serve`, `update_obligations`, `close`) и `ContourDemand`.
- [x] 1.2 `src/assembly/contour.py`: `ContourSettings` и `TradingContour` (`build`, `demand`, `run`, `finish`, `close`); сюда переезжают `_build_runtime`, `_Runtime`, `_run_bot`, `_finish_history` и их помощники (`_risk_limits`, `_load_contracts_metadata`, `_strategy_map`, `_session_calendar_provider`, `_dispatch_evidence`, `_OutboxExecutor`, `_broker_event`, `_instrument_names` и другие, используемые только торговой частью).
- [x] 1.3 Сбор `ContourDemand` на стороне контура: инструменты исполнения с ролями жизненного цикла, алиасы, обязательства, запросы истории и нижние границы исполнения — код из `_start_live_ingestion` и `post_tick` без изменения правил.
- [x] 1.4 `src/assembly/core.py`: `LiveCore` (`start_early`, `ensure_market_data`, `start_api`, `cluster_gate`, `serve`, `update_obligations`, `close`); сюда переезжают `_start_early_market_data`, `_register_saved_roster`, `_build_market_data` (общий с историей), `_start_live_ingestion` (без чтения базы), `_retire_unselected_pairs`, `_live_startup_waiter`, `_start_local_api`, `_cluster_redis`, обработчик паттернов.
- [x] 1.5 `src/assembly/history.py`: `HistoricalMarketServices` — данные прогона, границы предпроверки, кластерный вердикт по копии хранилища; `serve`/`update` пустые.

## 2. Точка входа

- [x] 2.1 `run.py`: `_launch` собирает «службы → контур → serve → run → close» в прежнем порядке запуска и остановки; выбор инструментов, план цепочки, расписание и предпроверка истории остаются в `run.py` без изменений.
- [x] 2.2 `_stop_early_services` заменён на `_stop_core`; ветки отмены запуска (код `2`, исключения при выборе) закрывают ядро через `LiveCore.close`.
- [x] 2.3 В `run.py` не остаётся функций, перенесённых в `src/assembly/`; нет реэкспортов ради тестов.

## 3. Тесты

- [x] 3.1 Подмены `run.<имя>` перенесённых функций переведены на `src.assembly.<модуль>.<имя>`; проверки не меняются; поиск `run.<перенесённое имя>` в `tests/` пуст.
- [x] 3.2 Тест направления импортов: `core.py`, `history.py`, `services.py` не импортируют `contour.py`.
- [x] 3.3 `ContourDemand` по финансовой базе с открытыми сделками совпадает с тем, что прежде передавал `_start_live_ingestion` (обязательства, запросы истории, нижние границы).
- [x] 3.4 `HistoricalMarketServices` не создаёт полосы, приём, поток, API и обработчик паттернов.
- [x] 3.5 Порядок остановки ядра и контура совпадает с прежним (подмены с записью порядка вызовов).
- [x] 3.6 Интеграционные тесты истории, в том числе `test_repeated_run_is_identical`, проходят; правки в них — только адреса подмен, проверки не менялись.
- [x] 3.7 Полный `pytest`.

## 4. Документация

- [x] 4.1 `ARCHITECTURE.md`: модуль `assembly` в таблице модулей, поток сборки «службы → контур».
- [x] 4.2 `docs/architecture/core-contours.md`: П2 отмечен как выполненный.

## 5. Найдено по ходу

- [x] 5.1 `tools/backfill_contract_factors.py` импортировал `_token_client_context` из `run.py` — переведён на `src.assembly.contour`.
- [x] 5.2 Подмены, которые после переноса стали бы работать вхолостую (`FUTURE_STRATEGIES`, `SHARE_STRATEGIES`, `_family_warmup`, `_strategy_map`, `ACTIVE_TIMEFRAMES`, `MARKET_DATA_SETTINGS`, `TINKOFF_TOKEN`, `log`), переведены туда, где значение читается.
- [x] 5.3 `tests/conftest.py`: ключи локального API из `.env` разработчика в тестах всегда подменены пустыми; проверка ключа в тесте не печатает значение при сбое.
