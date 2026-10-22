## 1. Хранилище покрытия

- [x] 1.1 `src/data/coverage_states.py`: `FINAL_EVIDENCE_STATES` (`confirmed_no_trade`, `source_absent`) и `WAITING_INTERVAL_STATES` (`pending`, `unresolved`).
- [x] 1.2 `MarketDataStore.set_interval_state`/`set_interval_states`: ожидание поверх итога пропускается без события в ленте; `received` и `scheduled_closed` ведут себя как прежде.
- [x] 1.3 `CompletenessLane._record`: в строке «Нет у источника» фактическое число проверок.

## 2. Тесты

- [x] 2.1 `tests/unit/data/test_final_coverage_states.py`: повторная сверка хвоста, перезапуск после «у источника нет» (без события в ленте), одиночная запись, свеча после итога, пересмотр планового перерыва, префикс через доказанные минуты.
- [x] 2.2 `pytest` по `tests/unit/data`, `tests/unit/market_calendar`, сверке, хранилищу и покрытию; полный `pytest`.

## 3. Живая база

- [x] 3.1 Решение владельца: разовое восстановление итогов, пониженных до исправления (запрос в отчёте сессии), или ожидание третьей проверки полосы полноты (до 19.10 ~19:31Z для PHOR). Выбрано разовое восстановление: `restore_final_coverage.py` прогнан на живой базе 19.10 после выкладки исправления, с копией базы.
