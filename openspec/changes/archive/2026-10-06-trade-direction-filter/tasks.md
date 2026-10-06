## 1. Конфигурация направлений

- [x] 1.1 Добавить `"directions": "directions"` в `_SECTIONS["trading"]` и `"directions": dict` в `_EXPECTED_TYPES` (`src/config_loader.py`)
- [x] 1.2 Добавить валидатор `_validate_directions` по образцу `_validate_risk_limits`: каждый ключ — тип инструмента, значение — непустой список строк строго из `{"long", "short"}` (нижний регистр); нарушения дают `ConfigError` «файл: [trading.directions] <тип> …» с проблемным ключом
- [x] 1.3 Подключить `_validate_directions` в общий цикл очистки конфига (рядом с веткой `risk_limits` в `config_loader.py`)
- [x] 1.4 Экспортировать `TRADING_DIRECTIONS = dict(_CONFIG.get("directions", {}))` в `src/config.py`
- [x] 1.5 Добавить в `default.toml` документирующий комментарий блока `[trading.directions]` (пример `future = ["long", "short"]`, `share = ["long"]`; пояснение про дефолт «обе стороны» при отсутствии блока), без изменения поведения по умолчанию

## 2. Допуск входа в TradeManager

- [x] 2.1 Добавить в `_REJECTION_MESSAGES` (`src/trade_management/models.py`) код `direction-not-allowed` с русским описанием «направление не разрешено для этого типа инструмента»
- [x] 2.2 Добавить в конструктор `TradeManager` параметр `direction_limits` (дефолт `None`) и нормализовать его в `Mapping[str, frozenset[str]]` (пустота/отсутствие типа ⇒ обе стороны разрешены)
- [x] 2.3 В начале `_plan_entry` проверять направление `decision.signal_type` (BUY → `long`, SELL → `short`) по `direction_limits` для `instrument.instrument_type` и возвращать недопуск `direction-not-allowed` без построения плана/заявки/резерва
- [x] 2.4 Убедиться, что ветка owned-позиции (`_manage_owned`) выполняется до проверки направления и фильтром не блокируется (управление позицией — стопы/цели/добор — не ограничивается)

## 3. Проводка в run.py

- [x] 3.1 Импортировать `TRADING_DIRECTIONS` в `run.py`
- [x] 3.2 Передать `direction_limits=TRADING_DIRECTIONS` в конструктор `TradeManager`

## 4. Тесты

- [x] 4.1 Добавить unit-тесты конфигурации в `tests/unit/test_config_loader.py` (класс `TestDirectionsConfiguration`): отсутствие секции ⇒ обе стороны; частичная таблица; неверный элемент/регистр/пустой список/несписковое значение ⇒ `ConfigError` с ключом
- [x] 4.2 Добавить unit-тесты допуска в `tests/unit/trade_management/test_signal_pipeline.py` (или `test_manager.py`): запрещённый шорт акции ⇒ `direction-not-allowed`; разрешённые лонг акции и обе стороны фьючерса ⇒ допуск продолжается; незаданный тип ⇒ обычный допуск; управление открытой позицией фильтром не блокируется
- [x] 4.3 Проверить, что существующие сценарии недопуска не изменились (напр. `test_actions_for_signal_rejects_exhausted_risk_budget`) — фильтр не влияет без заданного `direction_limits`

## 5. Верификация

- [x] 5.1 `.venv/bin/python -m pytest -q` — все тесты зелёные
- [x] 5.2 `.venv/bin/python -m ruff check .` (допустимы существующие ошибки в `tools/` и `openspec/_check_sync.py`)
- [x] 5.3 `openspec validate trade-direction-filter --strict` — spec-дельта корректна