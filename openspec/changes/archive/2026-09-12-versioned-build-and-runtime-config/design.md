## Context

См. proposal.md — Why. Продукт — PyInstaller one-file сборка (`run.spec`, `name='run'`), сейчас неверная никак не управляется: версии нет, бинарник анонимный, всё в `src/config.py` зашито кодом. Python ≥3.11 (домашняя стадия; `tomllib` из stdlib). Секреты — `python-dotenv` при импорте `src.config`. Dev-запуск — `python run.py`, PyInstaller-запуск — бинарник `run`.

## Goals / Non-Goals

Goals:
- Версия SemVer с единственным источником правды в коде; печать версии в лог при старте.
- Имя итоговой сборки прошло через версию: `robot-v<версия>`.
- Параметры времени выполнения загружаются из внешнего `robot.toml` (рядом с exe) с fallback на вшитые в сборку дефолты; в dev — `robot.toml` из корня проекта.
- Токены остаются вне конфиг-файла (`.env`/env); рабочий `robot.toml` распространяется рядом с exe без секретов.

Non-Goals: не автоматизировать выпуск (никаких build/bump-скриптов) — все шаги релиза ручные; не менять валидацию привязок стратегий (остаётся ведением `trading_bot._validate`); не вводить схему конфигурации с поддержкой нескольких каналов уведомлений; не выносить `TIMEFRAMES` и `BAR_TIME_TZ_OFFSET_HOURS` из кода.

## Decisions

### D1. Версия: `src/__init__.py`, PyInstaller `--name` — а не отдельный модуль версии
`__version__` в `src/__init__.py` (PEP 396): импортируется без чтения файлов, файл пуст сейчас — нет причины плодить `_version.py`. Правка только одной строки вручную при выпуске. Имя сборки задаётся при сборке переменной окружения `PYINSTALLER_NAME="robot-v<версия>"` — PyInstaller 6.x не принимает `--name` вместе со spec-файлом, поэтому `run.spec` читает `NAME` из env: `name = os.environ.get('PYINSTALLER_NAME') or 'run'` (дефолт для dev-сборок).

### D2. Папка приложения: `dirname(sys.executable)` при `sys.frozen`, корень проекта в dev
Хелпер `_app_dir()`: если `getattr(sys, "frozen", False)` — `os.path.dirname(sys.executable)`; иначе — `Path(__file__).resolve().parents[1]` (корень проекта). Именно здесь ищется внешний `robot.toml`. Альтернатива — `cwd` — отклонена: запуск из другой папки (ярлык/планировщик) дал бы нестабильный поиск.

### D3. Порт приоритетов конфигурации
Внешний `robot.toml` (`_app_dir()`/`robot.toml`) → вшитые дефолты (`default.toml` в `sys._MEIPASS`, только для сборки) → константы кода как последний якорь. В dev цепочка: корень проекта → константы кода. Загрузку/парсинг выносим в отдельный модуль `src/config_loader.py`, чтобы `src.config` остался тонким фасадом (сохраняются контракты импорта `from src.config import ...`). Приоритет для токенов не меняется: env > `.env` (поведение `python-dotenv`).

### D4. TOML-схема и жёсткость
`tomllib.load` на выборку, применение по секциям, **fail-fast**: незнакомый ключ, неверный тип, недопустимое значение канала (`telegram|console`) — `ConfigError` с текстом «исправьте robot.toml». Привязки стратегий — массивы строк, валидируются по именам реестра (существующая проверка в `trading_bot._validate` остаётся последним рубежом). Регистр таймфрейма `"1h"` проверяется по `TIMEFRAMES`.

Маппинг `config.py` → `robot.toml`:

| Секция TOML | Ключ | config.py |
|---|---|---|
| `[robot]` | `timeframe` | `TIMEFRAME` |
| `[robot]` | `sleep_seconds` | `SLEEP_SECONDS` |
| `[robot]` | `heartbeat_every_ticks` | `HEARTBEAT_EVERY_TICKS` |
| `[tick]` | `poll_secs` / `timeout_secs` | `TICK_POLL_SECS` / `TICK_TIMEOUT_SECS` |
| `[instruments]` | `fallback_type` / `fallback_ticker` | `INSTRUMENT_TYPE` / `TICKER` |
| `[notifier]` | `channel` | `NOTIFIER` |
| `[strategies.share]` | `<тикер>` → список имён | `SHARE_STRATEGIES` |
| `[strategies.future]` | `<код>` → список имён | `FUTURE_STRATEGIES` |

В коде остаются: `TIMEFRAMES` (словарь CandleInterval), `BAR_TIME_TZ_OFFSET_HOURS`, токены.

### D5. Вшитые дефолты в one-file: `datas` + `sys._MEIPASS`
`default.toml` (копия актуальных дефолтов конфиг-схемы) лежит в корне репозитория. В `run.spec` добавить `datas += [(os.path.join(SPECPATH, 'default.toml'), '.')]`. При запуске сборки файл распаковывается в `sys._MEIPASS` — читаем `Path(sys._MEIPASS)/'default.toml'` только когда `sys.frozen`. One-file, поэтому каталога данных рядом с exe нет.

### D6. Release-файлы рядом с бинарником
При выпуске вручную: `PYINSTALLER_NAME="robot-vX.Y.Z" pyinstaller run.spec` → `dist/robot-vX.Y.Z`; рядом создаётся `robot-vX.Y.Z.txt` — секция из `CHANGELOG.md` копируется руками (никакого скрипта, это договорённость). `CHANGELOG.md` — корень репозитория, Keep a Changelog: `## Unreleased` накапливает, релиз переименовывает секцию. Лог версии: в `run.py` в самом начале цикла (`logger.info` строкой «Робот v1.3.0 запущен»).

## Risks / Trade-offs

- **Расхождение вшитых дефолтов с кодом** при эволюции схемы → `default.toml` ревьювится в том же review, что и правки `config.py`; тесты сверяют невычисляемые дефолты явными случаями.
- **One-file и `sys._MEIPASS`** — сборка распаковывается в temp; если пользователь вручную рядом с exe положил `robot.toml` — он в приоритете, путь корректен при любом cwd. → явный unit-тест «внешний конфиг перекрывает дефолт».
- **Frozen-интерпретатор и `tomllib`** — в build включён тот же Python ≥3.11 → stdlib доступен; иначе сборка упадёт при старте с явной ошибкой импорта.
- **Отсутствие секции в `robot.toml`** — загружаются только присутствующие ключи, остальное — вшитые дефолты (не fail на незаполненные секции).
- **Жёсткость TOML** может раздражать получателя → сообщения об ошибке с номером строки и именами допустимых ключей.

## Migration Plan

1. `src/__init__.py` + `src/config_loader.py`; `config.py` подключить loader; старые тесты конфига обновить (значения теперь проходят через файлы/дефолты).
2. `default.toml` в корень; `run.spec` — `datas` для `default.toml`; рядом с собранным бинарником подготовить рабочий `robot.toml` (без токенов).
3. `run.py` — лог версии при старте; при необходимости передача не нужна (config остаётся фасадом импортов).
4. Ручная проверка: dev-запуск с project-root `robot.toml`, запуск собранного бинарника с `robot.toml` рядом и без (fallback на дефолты).
5. Откат (если возникли проблемы у получателя): удалить `robot.toml` рядом с бинарником → сборка работает на вшитых дефолтах.

## Open Questions

Нет существенных — все решения, влияющие на спеки/пайплайн задач, зафиксированы выше.