# Tasks: Профили фильтрации и каскадная привязка стратегий

## 1. Контракты и конфигурация

- [x] 1.1 Добавить frozen dataclass `Assignment(strategy: StrategyName, filter_profile: str)` в `src/strategies/contracts.py`
- [x] 1.2 Расширить `config_loader.py`: таблицы тикеров `[strategies.share.<ТИКЕР>]`/`[strategies.future.<БАЗА>]` с гибридным массивом `strategies` (строка → дефолтный профиль `basic_levels`; инлайн-таблица → обязательный `name`, опциональный `filter`); неизвестные ключи (в т.ч. `timeframe`/`tf`), иные типы элементов, отсутствие `name`, прежний плоский синтаксис → `ConfigError` с указанием файла и ключа
- [x] 1.3 Обновить `src/config.py`: `SHARE_STRATEGIES`/`FUTURE_STRATEGIES` → `dict[str, list[Assignment]]`
- [x] 1.4 Мигрировать `default.toml` и `robot.toml` на новую схему (состав стратегий сохранить, профили — дефолтные)

## 2. Профили фильтрации

- [x] 2.1 Создать `src/decision/filters/`: протокол `ProfileFilter` (`apply(decision, ctx) → Decision`), `NullFilter` (профиль `raw` — pass-through без обогащения)
- [x] 2.2 Перенести текущую логику `SignalFilter` в `BasicLevelsFilter` (профиль `basic_levels`) без изменения поведения
- [x] 2.3 Переделать `SignalFilter` в фасад: реестр `_PROFILES` = {`raw`, `basic_levels`}, `apply(decision, ctx, profile_name="basic_levels")` — фабричный lookup (неизвестное имя → ошибка с перечнем профилей) + делегирование

## 3. Валидация и оркестратор

- [x] 3.1 Расширить `validate_assignments` (`src/strategies/registry.py`): проверка имён профилей против реестра фильтров, ошибка с перечнями неизвестных/доступных, до старта цикла
- [x] 3.2 Обновить `TradingBot._strategies_for`/`_analyze`: привязки вместо имён; фильтр вызывается с `profile_name` привязки; `filtered_out` = (raw BUY/SELL) ∧ (filtered HOLD)
- [x] 3.3 Расширить `ExecutionPort.execute` и `NotifyOnlyExecutionPort` keyword-параметрами `filter_profile=""`, `filtered_out=False` с пробросом в `notify_decision`

## 4. Нотификации

- [x] 4.1 `DecisionFormatter.format`: параметры `filter_profile`/`filtered_out`; блок `[<профиль>]` после имени стратегии (опускается при пустом); статус `❌ Отклонено фильтром.` (без цены) при `filtered_out=True`; статусы BUY/SELL/HOLD без изменений
- [x] 4.2 `AbstractNotifier.notify_decision`: keyword-параметры `filter_profile`/`filtered_out` с пробросом в форматтер

## 5. Тесты

- [x] 5.1 Unit-тесты `config_loader`: обе формы элементов, каскад дефолта, все ошибочные формы (нет `name`, ключ `tf`/`timeframe`, плоский синтаксис, иной тип элемента)
- [x] 5.2 Unit-тесты фильтров: `raw` (проходит против тренда, без обогащения), `basic_levels` (поведение как ранее), фасад (дефолт, неизвестный профиль → ошибка с перечнем)
- [x] 5.3 Unit-тесты форматтера: блок `[профиль]` (есть/нет), `❌ Отклонено фильтром.` без цены; обновить существующие ожидания строк
- [x] 5.4 Unit-тесты бота/валидации: дубли стратегии с разными профилями, неизвестный профиль останавливает запуск, `filtered_out` вычисляется и пробрасывается в порт
- [x] 5.5 Прогон полного набора: `pytest tests/unit` + `pytest tests/snapshot` (snapshot-раннер без изменений)

## 6. Выпуск 2.0.0

- [x] 6.1 `src/__init__.py`: `__version__ = "2.0.0"` (MAJOR — ломается формат `robot.toml`)
- [x] 6.2 `CHANGELOG.md`: секция 2.0.0 с инструкцией миграции конфигурации «до/после»
- [x] 6.3 `ARCHITECTURE.md`: строка модуля `decision` — фильтр с профилями и фабрикой; пример привязки в обзоре
