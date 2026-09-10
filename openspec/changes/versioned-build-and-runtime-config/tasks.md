## 1. Версионирование продукта

- [x] 1.1 Создать `src/__init__.py` с `__version__ = "1.0.0"` и комментарием-правилами SemVer (MAJOR/MINOR/PATCH)
- [x] 1.2 В `run.py` при старте логировать строку с версией (`from src import __version__`; «Робот v… запущен»)
- [x] 1.3 Создать `CHANGELOG.md` в корне в формате Keep a Changelog с секцией `## Unreleased`

## 2. Загрузчик конфигурации из `robot.toml`

- [x] 2.1 Создать `src/config_loader.py`: `_app_dir()` (`dirname(sys.executable)` при `sys.frozen`, корень проекта в dev), загрузка `robot.toml` из папки приложения с fallback на вшитые `default.toml` (`sys._MEIPASS`) и константы кода
- [x] 2.2 В `src/config_loader.py` реализовать `load_config()`: чтение секций `[robot]`, `[tick]`, `[instruments]`, `[notifier]`, `[strategies.share]`, `[strategies.future]` и их маппинг в общепринятые ключи (таблица из design D4)
- [x] 2.3 В `src/config_loader.py` реализовать fail-fast `ConfigError`: незнакомый ключ, неверный тип, недопустимый канал (`telegram|console`) — ошибка с понятным текстом; таймфрейм проверяется по `TIMEFRAMES`
- [x] 2.4 Обновить `src/config.py`: константы (`TIMEFRAME`, `SLEEP_SECONDS`, `HEARTBEAT_EVERY_TICKS`, `TICK_POLL_SECS`, `TICK_TIMEOUT_SECS`, `INSTRUMENT_TYPE`, `TICKER`, `NOTIFIER`, `SHARE_STRATEGIES`, `FUTURE_STRATEGIES`) читаются через loader; сохраняются прежние имена, типы и импортируемость; токены по-прежнему из `.env`/env, без изменений

## 3. Дефолты, образец и сборка

- [x] 3.1 Создать `default.toml` в корне с актуальными дефолтами конфиг-схемы (все секции из design D4)
- [x] 3.2 Подготовить готовый `robot.toml` рядом с собранным бинарником в `dist/` (все секции с актуальными значениями, без токенов)
- [x] 3.3 В `run.spec` добавить `datas` для `default.toml`: `datas += [(os.path.join(SPECPATH, 'default.toml'), '.')]`
- [x] 3.4 Проверить ручную сборку: `PYINSTALLER_NAME="robot-v1.0.0" pyinstaller run.spec` создаёт `dist/robot-v1.0.0`

## 4. Unit-тесты

- [x] 4.1 В `tests/unit/` добавить `test_config_loader.py`: резолвинг папки (frozen vs dev), внешний `robot.toml` перекрывает вшитые дефолты, отсутствие файла → дефолты, незнакомый ключ → `ConfigError`, недопустимый канал → `ConfigError`
- [x] 4.2 Добавить `test_version.py`: `from src import __version__` возвращает строку `MAJOR.MINOR.PATCH`
- [x] 4.3 Прогнать полный набор тестов (`pytest`) и ruff; все проверки зелёные