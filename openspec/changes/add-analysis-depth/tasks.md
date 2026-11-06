## 1. Конфигурация и глубина

- [ ] 1.1 `src/config_loader.py`, `src/config.py`, `default.toml`: ключ `[robot] analysis_depth` (целое 50…1000, по умолчанию 150; `bool` и нецелое — `ConfigError` с `robot.analysis_depth`), константа `ANALYSIS_DEPTH`.
- [ ] 1.2 `src/history/warmup.py`: `binding_depths(binding, strategy, profiles, triple_screen, ladder, depth)` — `max(минимум, depth)` для рабочего ТФ и старших ТФ triple_screen; `obligation_requirements` поднимает глубину сопровождения до `depth`; `live_data_requirements` и `warmup_requirements` отдают глубину. `binding_requirements` не меняется.

## 2. Живой цикл

- [ ] 2.1 `src/bot/live_analysis.py`: окно расчёта — наибольший покрытый суффикс до глубины (общая функция); `_calculate`, `_input_ready`, `_context` и кадр сопровождения получают это окно; `_quality` проверяет минимум и покрытие минимума.
- [ ] 2.2 `register_needs_for`: прогрев запрашивает глубину; для каждой пары контур передаёт ядру и глубину, и минимум допуска.
- [ ] 2.3 `src/data/htf_provider.py`: `frame_for(..., min_bars, depth)` — live: `prepare_live_history(depth)`, окно — покрытый суффикс до `depth`, отказ только при окне короче `min_bars`; history: `tail(depth)`. `src/decision/filters/triple_screen.py` передаёт глубину из `binding_depths`.

## 3. Ядро и рыночная база

- [ ] 3.1 `src/data/market_store.py`: колонка `contour_demand.required_bars` (минимум допуска рядом с глубиной `min_bars`), миграция с подъёмом `SCHEMA_VERSION`: существующие строки получают `required_bars = min_bars`; запись и чтение спроса — с обеими величинами (`src/data/follower.py`, контурная сторона).
- [ ] 3.2 `src/data/ingestion.py`: исчерпанные окна прогрева (`WARMUP_DAYS` кончились или расширение не дало ранних свечей) при закрытых свечах не меньше минимума — прогрев `done` без `history_error`; ошибка остаётся только при недоборе минимума.

## 4. Исторический цикл

- [ ] 4.1 `src/bot/trading_bot.py`: в `_tick` кадр рабочего ТФ режется до глубины перед `_analyze` и `_lazy_context`; `_manage` — до max(глубина привязок ТФ, глубина сопровождения).
- [ ] 4.2 `src/data/cache.py` `prepare_history` и `src/history/contract_lifecycle.py`: история догружается до глубины (не раньше `first_trade_date`), готовность к активации — по минимуму; стартовая загрузка прогона расширяется до глубины мягко.
- [ ] 4.3 Предпроверка прогона (`src/history/preflight.py`) требует минимум, как сейчас; проверить, что поведение не изменилось.

## 5. Тесты и проверка

- [ ] 5.1 Unit: `binding_depths` (рабочий ТФ, triple_screen 15m → 4h/1d, сопровождение ma_cloud 41, `analysis_depth` ниже минимума); `_calculate` получает ровно 150 свечей при длинном кадре и 80 при кадре в 80; `_quality` допускает 60 свечей при глубине 150 и минимуме 50; окно обрезается пробелом 120 свечей назад и не допускается при пробеле 30 свечей назад; `HtfProvider` в live и в history даёт одно окно; прогрев ядра без ошибки при исчерпанных окнах и набранном минимуме; конфиг — умолчание, границы, тип; миграция `contour_demand`.
- [ ] 5.2 Эквивалентность циклов: один и тот же кадр через `LiveAnalysis._calculate` и через исторический `_tick` даёт одинаковые решение, тренд контекста и экраны triple_screen.
- [ ] 5.3 Снапшот `tests/snapshot/test_strategies.py`: окно `analysis_depth` = полный префикс на барах после `analysis_depth + 50` для всех кейсов с достаточной фикстурой; эталоны не меняются (`pytest tests/snapshot` без `--update-snapshots` зелёный).
- [ ] 5.4 Полный `pytest` и `ruff check`.
- [ ] 5.5 Сравнительный прогон истории на тестовом роботе (8102): один диапазон (NG и BR, 15m/1h, 4–6 недель) до и после. Ожидание: сделки почти совпадают, потому что история уже считала по всему кадру. Записать число сделок, R и время прогона: после изменения прогон должен стать не медленнее.
- [ ] 5.6 Эмуляция на тестовом роботе (8102) после рестарта: прогрев пар 15m/1h без лишних запросов, 4h/1d — с расширением окон и без ошибки прогрева; сигналы macd и ma_cloud на свежих барах совпадают с расчётом по всему ряду (сверка скриптом по журналу решений); время шага анализа не выросло заметно.

## 6. Документы

- [ ] 6.1 `docs/live-session-analysis.md`: минимум допуска, глубина расчёта, окно и покрытие; `docs/historical-mode.md`: окно истории равно живому, оговорка о пробелах без покрытия; `docs/market-data/live-ingestion.md`: `required_bars` в спросе, прогрев до глубины без ошибки.
- [ ] 6.2 `CHANGELOG.md`: живые сигналы macd_rsi_stoch и ma_cloud_rsi_macd совпадают с историей; ma_cloud входит заметно чаще; новый ключ `analysis_depth`.
- [ ] 6.3 Если `add-indicators-api` уже влит: режим `strategy` API индикаторов берёт `binding_depths`, `depth` в ключе кэша — глубина расчёта; открытый вопрос 2 в его `design.md` закрывается ссылкой на это изменение.
