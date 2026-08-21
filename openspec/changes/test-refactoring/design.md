# Дизайн: реорганизация дерева тестов

## Context

Текущее дерево (после смен snapshot-tests и flatten-test-data-layout):

```
tests/
├── conftest.py                      # --update-snapshots, общие фикстуры
├── test_retry.py, test_runner.py,
│   test_selector.py, test_signals.py  # unit-тесты в корне
├── strategies/
│   └── test_macd_rsi_stoch.py       # snapshot-тест (glob tests/data/*)
└── data/
    ├── BR_1h/{candles.csv, macd_rsi_stoch_expected_signals.csv}
    └── NG_1h/{candles.csv, macd_rsi_stoch_expected_signals.csv}
```

Ограничения: 99 тестов зелёные; pre-commit хук запускает `pytest` из корня; тесты импортируют только `src.*`; эталоны зафиксированы в Git.

## Goals / Non-Goals

**Goals:**
- Физически разделить `tests/unit/` и `tests/snapshot/` без изменения поведения тестов.
- Перенести артефакты данных под `tests/snapshot/data/` с сохранением git-истории.
- Сохранить работу pre-commit хука и полный прогон без правки хука.

**Non-Goals:**
- Маркеры pytest (`-m unit/-m snapshot`) — разделение чисто каталоговое.
- Изменение логики расчёта сигналов, формата эталонов или лимита 300 свечей.
- Новые unit-тесты (например `test_indicators.py`) — только размещение существующих.

## Decisions

- **D1. Каталоги вместо pytest-маркеров.** Разделение достигается структурой путей: `pytest tests/unit`, `pytest tests/snapshot`, `pytest` — всё. Альтернатива (`@pytest.mark.unit` + `-m`) отвергнута: дублирует каталог, требует дисциплины маркировки каждого нового теста и усложняет конфигурацию без выигрыша для проекта из 5 файлов.
- **D2. Перемещение через `git mv`.** Все переносы файлов выполняются `git mv` — история и blame сохраняются. Альтернатива (копирование + удаление) ломает переименование в diff.
- **D3. Один источник путей в коде.** В `tests/snapshot/test_macd_rsi_stoch.py` базовый путь вычисляется от файла теста (`Path(__file__).parent / "data"`) вместо хардкода строки `"tests/data"`. В `tools/download_snapshot_data.py` константа `DATA_DIR = Path("tests") / "snapshot" / "data"`. Альтернатива (относительный путь от cwd) отвергнута: pytest может запускаться с другим `rootdir`.
- **D4. Conftest остаётся корневым.** Опция `--update-snapshots` определена один раз в `tests/conftest.py`; перенос её в `tests/snapshot/conftest.py` сломал бы запуск опции при выборочном прогоне из других путей и создал бы второй conftest без необходимости.
- **D5. Папка `tests/strategies/` удаляется после переноса единственного файла.** Правило «один файл на стратегию» сохраняется, но внутри `tests/snapshot/`.
- **D6. Обновление блока контекста `openspec/config.yaml`.** Правила наименования в контексте переписываются под новые пути (`tests/snapshot/test_<стратегия>.py`, `tests/snapshot/data/<кейс>/...`) — иначе будущие смены будут ориентироваться на устаревшее дерево.

## Risks / Trade-offs

- [Слом discovery при промежуточных состояниях переноса] → перенос выполняется одной атомарной серией `git mv`, сразу за ней — правка glob-путей; полный прогон после каждого шага tasks.
- [Забытый относительный путь в скрипте скачивания] → проверка идемпотентности скрипта (повторное скачивание NG) входит в задачи.
- [Дублирование данных при неудачном merge] → `git mv` гарантирует rename-детект; старая папка удаляется целиком.
- [Хуки/инструкции ссылаются на `tests/data`] → grep по репозиторию на упоминания старых путей включён в задачи проверки.

## Migration Plan

1. `git mv` unit-тестов в `tests/unit/`, snapshot-теста в `tests/snapshot/`, `tests/data` → `tests/snapshot/data`.
2. Правка glob-путей теста и констант скрипта скачивания.
3. Полный `pytest` (99 passed), затем выборочные прогоны `pytest tests/unit` и `pytest tests/snapshot`.
4. Проверка pre-commit хука коммитом.
5. Откат: обратные `git mv` + revert правок путей (один коммит).

## Open Questions

Нет — все решения зафиксированы выше.
