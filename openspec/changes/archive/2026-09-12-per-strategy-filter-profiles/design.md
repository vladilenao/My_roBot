# Design: Профили фильтрации и каскадная привязка стратегий

## Context

См. proposal.md — Why. Технические опорные точки текущего кода:

- Привязки: `SHARE_STRATEGIES`/`FUTURE_STRATEGIES` — `dict[str, list[StrategyName]]` (`config.py`), семантическая валидация — `validate_assignments` в реестре, fail-fast до старта цикла (`TradingBot._validate`).
- Фильтр: `SignalFilter` (`decision/filter.py`) — frozen dataclass, `apply(decision, ctx) → Decision`, логика блока против тренда зашита единственная.
- Доставка: `_analyze` → `_emit` → `ExecutionPort.execute(decision, instrument)` → `NotifyOnlyExecutionPort` → `notifier.notify_decision(decision, label)`; формат — `DecisionFormatter` (`● label (tf) HH:MM | strategy ➜ статус`), `timeframe` задан конструктором (глобальный `TIMEFRAME`).
- `config_loader` — строгий: неизвестная секция/ключ/тип → `ConfigError` с указанием файла.

## Goals / Non-Goals

**Goals:**
- Каскадная привязка «тикер × стратегия × профиль фильтра» в `robot.toml` с дефолтом профиля `basic_levels`.
- Фабричный метод выбора фильтра по имени профиля; профили `raw` и `basic_levels`; расширение — регистрацией, без правки цикла.
- Шаблон нотификации с блоком `[профиль]` и явным статусом отклонения фильтром.
- Fail-fast валидация имён стратегий и профилей при старте (не в рантайме тика).

**Non-Goals:**
- Мульти-ТФ ритм, `timeframe` на тикер, `tf` на стратегию (отдельный чейндж; в TOML-схеме эти ключи пока запрещены строгим загрузчиком).
- Новые профили фильтрации сверх `raw`/`basic_levels` — подключаются позже через фабрику.
- Эмодзи-маркеры профилей, изменение текстов статусов BUY/SELL/HOLD.
- Контракт `Decision` — без изменений.

## Decisions

### D1. `Assignment` — типизированное значение привязки
Новый frozen dataclass `Assignment(strategy: StrategyName, filter_profile: str)` в `src/strategies/contracts.py` — чистый модуль контрактов без зависимостей, его уже импортируют config/registry/bot. Словари становятся `dict[str, list[Assignment]]`. Дубликаты имени стратегии на тикере легальны при разных профилях; идентичность привязки — кортеж `(инструмент, стратегия, профиль)`.

*Альтернатива:* dict-привязки (`assignment.get("name")`, как в эскизе концепции) — отклонена: проект типизирован, строковые ключи не проверяются статически и разводят опечатки.

### D2. Разделение валидации: загрузчик — синтаксис, реестры — семантика
`config_loader` разбирает гибридный массив `strategies` по форме: строка → `Assignment(name, "basic_levels")`; инлайн-таблица → обязательный `name`, опциональный `filter`; неизвестные ключи (в т.ч. `timeframe`, `tf`) и старый плоский синтаксис (`SBER = [...]`) → `ConfigError`. Проверка «имя есть в реестре стратегий» и «профиль есть в реестре фильтров» остаётся семантикой запуска — расширенный `validate_assignments` до старта цикла (как сегодня для стратегий). Загрузчик не импортирует домен.

*Альтернатива:* Pydantic-схема — отклонена: в проекте уже есть строгий hand-rolled загрузчик без лишних зависимостей; Pydantic вернём, только если вложенность схемы вырастет.

### D3. Фабричный метод внутри `SignalFilter`
`src/decision/filters/`: протокол `ProfileFilter.apply(decision, ctx) → Decision`; `NullFilter` (профиль `raw` — возвращает `Decision` без изменений и обогащения); `BasicLevelsFilter` (профиль `basic_levels` — текущая логика `SignalFilter` переезжает без изменений). `SignalFilter` остаётся frozen dataclass-фасадом с реестром `_PROFILES: dict[str, ProfileFilter]`; `apply(decision, ctx, profile_name="basic_levels")` — фабричный lookup по имени (неизвестное имя → ошибка с перечнем профилей) и делегирование. Фильтры stateless → экземпляры хранятся прямо в реестре.

*Альтернатива:* отдельный `FilterRegistry` по образцу `strategies.registry` — отклонена как избыточная для двух профилей и одного потребителя; точка расширения сохранена таблицей `_PROFILES`.

### D4. «Отклонено фильтром» без расширения `Decision`
Оркестратор сравнивает решение до/после фильтра: `filtered_out = raw.signal_type in (BUY, SELL) and filtered.signal_type is HOLD`. Контекст доставки идёт keyword-параметрами с дефолтами по существующей цепочке: `_emit` → `ExecutionPort.execute(decision, instrument, *, filter_profile="", filtered_out=False)` → `notify_decision(..., filter_profile=..., filtered_out=...)`. Дефолты сохраняют совместимость прочих вызовов и тестов; `Decision` не расширяется.

*Альтернатива:* поля `filter_profile`/`filtered_out` в `Decision` — отклонена: контракт расширялся недавно (action/exit_*), фильтр-метаданные нужны только на границе доставки.

### D5. Форматтер: профиль — параметр вызова, не конструктора
`DecisionFormatter.format(decision, instrument_label, *, filter_profile="", filtered_out=False)`: блок `[<профиль>]` после имени стратегии, опускается при пустом значении (консистентно с прочими опциональными блоками); статусы BUY/SELL/HOLD без изменений; при `filtered_out=True` — статус `❌ Отклонено фильтром.` без цены. Таймфрейм — из конструктора (глобальный `TIMEFRAME`).

### D6. Инстанс стратегии общий для дублей
Дубли имени на тикере делят инстанс кэша стратегий: `compute`/`decide` детерминированы одним фреймом, внутреннее состояние (ma_cloud_rsi_macd) — функция истории, у дублей совпадает. Профиль различает только пост-фильтрацию. Если будущему профилю понадобится влиять на `decide` — это уже другая стратегия с собственным `NAME`.

## Risks / Trade-offs

- Опечатка в имени профиля в `robot.toml` → fail-fast на старте через `validate_assignments` (ошибка с перечнем доступных профилей), а не молчаливый fallback.
- BREAKING формата `robot.toml` → одношаговая миграция: старый синтаксис падает с явным `ConfigError`, CHANGELOG 2.0.0 содержит пример «до/после».
- Рост числа привязок при дублях (одна стратегия × 2 профиля) → издержки линейны и малы (общий инстанс, фильтр дёшев); при появлении тяжёлых профилей — кэшировать `compute` по (инструмент, стратегия).
- Смена наблюдаемого формата сообщений → unit-тесты форматтера/нотификаторов обновляются в том же чейндже; спека `strategy-contract` освежается (там зафиксирован устаревший текст «🚀 ПОКУПАТЬ!»).

## Migration Plan

1. `default.toml` и `robot.toml` — перевести секции `strategies.*` на таблицы тикеров с гибридным массивом (имена и покрытие сохранить).
2. Код: contracts (`Assignment`) → config_loader/config → validate_assignments → filters + SignalFilter → bot → execution port → formatter/notifiers.
3. Тесты: unit (конфиг, фильтр, форматтер, бот), snapshot-раннер не затрагивается.
4. Выпуск `2.0.0`: `src/__init__.py`, CHANGELOG с инструкцией миграции конфигурации.
5. Откат: вернуть прежний `robot.toml` и версию пакета — состояние внешних систем не затрагивается (бот notify-only).
