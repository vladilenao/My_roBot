# Snapshot-тесты торговых сигналов

## Why

Расчётный конвейер сигналов (`tech_analyze` → `get_last_signals` → `make_decision`) не имеет data-driven проверки: рефакторинг или обновление pandas/pandas-ta-classic может молча изменить торговые сигналы. Нужны детерминированные snapshot-тесты на зафиксированном отрезке истории, масштабируемые на несколько стратегий без правки кода тестов.

## What Changes

- Новая тестовая инфраструктура, готовая к нескольким стратегиям:
  - `tests/strategies/` — по одному файлу теста на стратегию;
  - `tests/data/<strategy_name>/<case>/` (например `tests/data/macd_rsi_stoch/NG_1h/`) — папка тест-кейса с фикстурой `candles.csv` (≤300 свечей) и эталоном `expected_signals.csv`.
- Скрипт скачивания фикстур `tools/download_snapshot_data.py` с жёстким лимитом глубины истории: глубина рассчитывается автоматически как `SIGNAL_WINDOW × 4` из `src.config` с абсолютным потолком 300 свечей; пост-обрезка `tail(limit)`; дефолтный диапазон `load_candles` (30 дней) не используется.
- `requirements-dev.txt` — отдельное тестовое окружение с жёстко запиненными текущими версиями pandas, pandas-ta-classic, numba и pytest.
- Флаг `pytest --update-snapshots`: вместо сверки тест пересчитывает сигналы боевым модулем и перезаписывает `expected_signals.csv`; изменения стратегии коммитятся осознанно через Git diff.
- Динамическая параметризация pytest: тест стратегии сканирует `tests/data/<strategy_name>/*`, имя подпапки становится именем тест-кейса; новый инструмент = новая папка, ноль правок кода.
- Динамический импорт `SIGNAL_WINDOW` и параметров прогрева из `src.config` — тест всегда синхронизирован с продакшн-конфигом.

## Capabilities

### New Capabilities

- `test-data`: однократное скачивание компактных исторических свечей из Invest API с жёстким лимитом глубины, формат и расположение файлов фикстур, пин версий тестового окружения.
- `snapshot-testing`: offline-прогон локальных свечей через боевой расчётный модуль, динамическая параметризация по папкам кейсов, сверка с эталоном через `pandas.testing.assert_frame_equal`, регенерация эталона флагом `--update-snapshots`.

### Modified Capabilities

_(нет — существующие домены api-client, configuration, data-loading, indicators, instruments, notification, orchestration, signals не меняются)_

Примечание: черновые файлы `openspec/specs/test-data/spec.md` и `openspec/specs/snapshot-testing/spec.md`, созданные в ходе исследования (/opsx-explore), уточняются данной сменой (структура папок под стратегии, механизм регенерации, формула глубины) и приводятся в соответствие при архивации.

## Impact

- **Новые пути**: `tests/strategies/`, `tests/data/`, `tools/download_snapshot_data.py`, `requirements-dev.txt`.
- **Без изменений**: рабочий код `src/**`, `run.py` — модули только импортируются (`tech_analyze`, `get_last_signals`, `make_decision`, `SIGNAL_WINDOW`).
- **Зависимости**: новых сторонних библиотек нет (pytest уже используется); версии pandas/pandas-ta-classic/numba фиксируются в `requirements-dev.txt` по фактически установленным.
- **Ограничения**: теханализ строго pandas + pandas-ta-classic; сеть разрешена только скрипту скачивания, pytest работает offline; объём скачивания на тест-кейс ≤300 свечей (NG, 1h).
