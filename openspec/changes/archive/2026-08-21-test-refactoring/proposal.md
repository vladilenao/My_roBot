# Реорганизация дерева тестов: unit / snapshot

## Why

Все тесты лежат в корне `tests/` вперемешку: быстрые изолированные unit-тесты и тяжёлые data-driven snapshot-тесты не разделены ни физически, ни концептуально. Это мешает запускать быстрое подмножество отдельно от медленного и размывает правило размещения новых тестов. Данные snapshot-тестов (`tests/data/`) висят рядом с кодом тестов вместо того, чтобы принадлежать своему подпространству.

## What Changes

- **BREAKING** Вводится строгое двухуровневое дерево: `tests/unit/` — быстрые изолированные тесты функций и математики (без файлов данных), `tests/snapshot/` — тяжёлые data-driven snapshot-тесты. Корень `tests/` перестаёт содержать файлы тестов.
- Существующие корневые тесты переносятся в `tests/unit/`: `test_retry.py`, `test_runner.py`, `test_selector.py`, `test_signals.py`.
- Snapshot-тесты переезжают из `tests/strategies/test_<стратегия>.py` в `tests/snapshot/test_<стратегия>.py` (правило «один файл на стратегию» сохраняется).
- Хранилище артефактов переносится с `tests/data/` на `tests/snapshot/data/`: кейс — папка `<ИНСТРУМЕНТ>_<ТАЙМФРЕЙМ>/` с ровно одним `candles.csv`; стратегия кодируется именем файла эталона `<strategy_name>_expected_signals.csv`. Уровень стратегии в путях данных запрещён.
- Скрипт скачивания (`tools/download_snapshot_data.py`) начинает писать в новые пути `tests/snapshot/data/<case>/`.
- Общий `conftest.py` (фикстуры + опция `--update-snapshots`) остаётся на уровне `tests/` и действует на оба подпространства.
- Блок контекста в `openspec/config.yaml` обновляется под новое дерево.

## Capabilities

### New Capabilities
- `test-layout`: правила организации дерева тестов — строгое разделение `tests/unit/` и `tests/snapshot/`, размещение общих фикстур в корневом conftest.

### Modified Capabilities
- `snapshot-testing`: требование «Динамическая параметризация по папкам кейсов» — новое расположение файлов тестов (`tests/snapshot/`) и данных (`tests/snapshot/data/*`).
- `test-data`: требования «Скрипт скачивания тестовых данных» и «Структура хранения фикстур» — пути записи/хранения переносятся под `tests/snapshot/data/`.

## Impact

- Перемещение файлов: `tests/*.py` (5 шт.) → `tests/unit/`; `tests/strategies/test_macd_rsi_stoch.py` → `tests/snapshot/`; `tests/data/*` → `tests/snapshot/data/*` (через `git mv` для сохранения истории).
- Правки кода: константы путей в `tools/download_snapshot_data.py`; glob-пути обнаружения кейсов в snapshot-тестах.
- Конфигурация: блок контекста в `openspec/config.yaml`.
- Pre-commit хук (запуск `pytest` из корня) продолжает работать без изменений — pytest рекурсивно находит тесты в подпапках.
- Импорты тестов из `src.*` не меняются; поведение всех 99 тестов сохраняется.
