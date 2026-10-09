## 1. Данные: дата экспирации в метаданных контракта

- [x] 1.1 Добавить `expiration_date: datetime | None` в `ContractMeta` (`src/portfolio/models.py`)
- [x] 1.2 Заполнять `expiration_date` в `load_futures_contracts` (`src/api/instruments.py`) из `f.expiration_date` нормализатором `to_naive`; для акций — `None`
- [x] 1.3 Проверить, что проброс метаданных (`broker.set_contracts`, `storage.set_contract_metadata`) не теряет новое поле и не ломает существующие тесты

## 2. Конфигурация

- [x] 2.1 Добавить `_DEFAULTS["contract_expiry_block_days"] = 2` и импортируемую константу в `src/config.py`
- [x] 2.2 `src/config_loader.py`: маппинг `_SECTIONS["trading"]["contract_expiry_block_days"]`, тип `int` в `_EXPECTED_TYPES`, валидация неотрицательности с `ConfigError` (по образцу `tick_catch_up_bars`)
- [x] 2.3 Добавить `contract_expiry_block_days = 2` в `[trading]` вшитого `default.toml`
- [x] 2.4 `run.py`: проброс константы в конструктор `TradeManager`

## 3. Логика управления сделками

- [x] 3.1 Хелпер близости экспирации (naive UTC, `(expiry - now).total_seconds() <= block_days * 86400`, `None` → False)
- [x] 3.2 `actions_for_signal`: сразу после проверки `no-contract-metadata` отклонять вход/добор с причиной `contract-expiring` и человекочитаемым описанием для близкой экспирации
- [x] 3.3 `manage()`: при близкой экспирации — `CancelEntry` для `ENTRY_PENDING` и `CloseTrade` для фаз `_MANAGEABLE` с `quantity > 0`, профильные действия пропустить

## 4. Тесты и проверка

- [x] 4.1 Unit-тесты конфигурации: дефолт `2` без ключа, переопределение, `ConfigError` на недопустимом значении
- [x] 4.2 Unit-тесты допуска: сигнал по фьючерсу вблизи экспирации отклоняется с `contract-expiring`; его добор также отклоняется; контракт вне порога не отклоняется
- [x] 4.3 Unit-тесты `manage()`: при близкой экспирации отменяется `ENTRY_PENDING` (`CancelEntry`) и закрывается открытая позиция (`CloseTrade`); контракт с `expiration_date=None` (акция) не затрагивается
- [x] 4.4 Прогнать `pytest -q` и `ruff check src tests` — зелёные