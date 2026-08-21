# Задачи: реорганизация дерева тестов

## 1. Перенос unit-тестов

- [x] 1.1 Создать `tests/unit/`; выполнить `git mv tests/test_retry.py tests/test_runner.py tests/test_selector.py tests/test_signals.py tests/unit/`
- [x] 1.2 Прогнать `pytest tests/unit` — все перенесённые тесты зелёные, файлы данных не читаются

## 2. Перенос snapshot-тестов и данных

- [x] 2.1 Выполнить `git mv tests/strategies/test_macd_rsi_stoch.py tests/snapshot/` и удалить пустую папку `tests/strategies/`
- [x] 2.2 Выполнить `git mv tests/data tests/snapshot/data`
- [x] 2.3 Обновить базовый путь обнаружения кейсов в `tests/snapshot/test_macd_rsi_stoch.py`: `Path(__file__).parent / "data"` (D3)
- [x] 2.4 Убедиться, что `tests/conftest.py` не содержит путей к данным; опция `--update-snapshots` работает из нового расположения (`pytest tests/snapshot --update-snapshots --co -q`)

## 3. Скрипт скачивания и конфигурация

- [x] 3.1 В `tools/download_snapshot_data.py` обновить константу пути записи на `tests/snapshot/data/<case>/...`
- [x] 3.2 Проверить идемпотентность скрипта повторным скачиванием NG_1h (файлы перезаписаны по новым путям, md5 candles.csv стабильны)
- [x] 3.3 Обновить блок контекста в `openspec/config.yaml` под новое дерево (`tests/unit/`, `tests/snapshot/test_<стратегия>.py`, `tests/snapshot/data/<кейс>/...`)

## 4. Верификация

- [x] 4.1 Полный прогон `pytest` — 99 passed; выборочные прогоны `pytest tests/unit` и `pytest tests/snapshot` дают согласованные подмножества
- [x] 4.2 Grep по репозиторию на устаревшие упоминания `tests/data` и `tests/strategies` вне архива openspec — правки или подтверждение отсутствия
- [x] 4.3 Проверить pre-commit хук коммитом: хук запускает pytest из корня, оба подпространства обнаруживаются
