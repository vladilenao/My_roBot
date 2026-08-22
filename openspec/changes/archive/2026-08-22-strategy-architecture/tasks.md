# Задачи: architecture/strategy-architecture

## 1. Каркас контракта и реестра

- [x] 1.1 Создать `src/strategies/base.py`: Enum `SignalType` (BUY, SELL, HOLD), dataclass `Decision(signal_type, price)`, Protocol `Strategy` с атрибутами NAME, STRATEGY_WINDOW и методами compute(df), decide(ta), expected_events(ta), required_history()
- [x] 1.2 Создать `src/strategies/__init__.py` с реестром: `register` (декоратор класса; повторный NAME — ошибка), `get_strategy(name)` (неизвестное имя — ошибка со списком доступных), `all_strategies()`; реестр пуст до импорта пакетов стратегий
- [x] 1.3 Написать unit-тесты `tests/unit/test_registry.py`: регистрация, получение по имени, ошибка неизвестного имени, ошибка дубликата NAME

## 2. Пакет стратегии macd_rsi_stoch

- [x] 2.1 Перенести математику через `git mv src/indicators/calculator.py` в `src/strategies/macd_rsi_stoch/indicators/` без редактирования формул; удалить старые пакеты `src/indicators/`, `src/signals/`
- [x] 2.2 Разнести логику `src/signals/generator.py`: агрегацию (rolling-сумма окна) → `macd_rsi_stoch/signals/aggregate.py`; тексты решений («🚀 ПОКУПАТЬ!» и др., округление цены) → новый общий `src/notifier/formatter.py`; удалить модуль generator
- [x] 2.3 Создать класс `MacdRsiStochStrategy` (`strategy.py`) с NAME="macd_rsi_stoch", STRATEGY_WINDOW из прежнего SIGNAL_WINDOW, методами compute/decide (возврат Decision(SignalType)) /expected_events (единая точка консенсус-правила)/required_history; зарегистрировать через @register
- [x] 2.4 Обновить unit-тесты: `test_signals.py` под новые пути и decide(); новый `test_formatter.py` (BUY/SELL/HOLD тексты, label, округление цены)

## 3. Движок и конфигурация

- [x] 3.1 Удалить глобальный SIGNAL_WINDOW из `src/config.py`; добавить `STRATEGY_ASSIGNMENTS: dict[ticker, list[strategy_name]]` с дефолтом для macd_rsi_stoch
- [x] 3.2 Переписать `scheduler/runner.py`: кэш свечей на тикер → перебор назначенных стратегий через get_strategy() → compute/decide → formatter → send_message; пропуск тикеров без привязки с записью в лог; try/except вокруг каждой пары «стратегия×тикер»
- [x] 3.3 Обновить `test_runner.py` под новый поток (моки реестра/отправки); прогнать `pytest tests/unit`

## 4. Snapshot-раннер и скрипт скачивания

- [x] 4.1 Переименовать существующий эталон NG_1h и BR_1h в `macd_rsi_stoch_expected_signals.csv` (git mv); создать общий `tests/snapshot/helper.py` (чтение CSV, assert_frame_equal с rtol, first_divergence, запись эталона)
- [x] 4.2 Создать единый параметризованный раннер `tests/snapshot/test_strategies.py` (glob `<case>/*_expected_signals.csv`, параметризация (кейс, стратегия), диспетчеризация get_strategy, окно у объекта стратегии); удалить файл-на-стратегию `test_macd_rsi_stoch.py`; убедиться, что оба кейса зелёные
- [x] 4.3 Переписать `tools/download_snapshot_data.py`: аргумент имя стратегии + кейс; эталон через `get_strategy(name).expected_events()`; проверка достаточности истории по `required_history()` перед записью файлов (иначе выход с ошибкой); дефолт глубины запроса — 300 (HARD_LIMIT); собственную математку эталона удалить
- [x] 4.4 Проверить идемпотентность скачивания (двойной запуск одного кейса → стабильные контрольные суммы файлов)

## 5. Верификация

- [x] 5.1 Полный `pytest` зелёный; grep на мёртвые импорты (`from src.indicators`, `from src.signals`, `make_decision`, `SIGNAL_WINDOW`) не даёт совпадений вне архива смен
- [x] 5.2 Контрольное добавление заглушки новой стратегии в реестр подтверждает: движок, раннер и скрипт работают с ней без правок их кода (проверка вручную, затем заглушку удалить)
