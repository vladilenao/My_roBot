# Tasks: ООП-рефакторинг пакета notifier

## 1. Новый пакет notifier

- [x] 1.1 Создать `src/notifier/base.py`: класс `DecisionFormatter` с методом `format(self, decision, instrument_label="") -> str` — тело перенести из `format_decision` без изменения текстов (префикс `[label] `, эмодзи, округление до 3 знаков)
- [x] 1.2 В `src/notifier/base.py` создать `AbstractNotifier(ABC)`: `__init__(self, formatter=None)` сохраняет форматтер (`DecisionFormatter()` по умолчанию); конкретный `notify_decision(decision, instrument_label="")` = `self.notify(self._formatter.format(decision, instrument_label))`; абстрактный `notify(message: str) -> None`
- [x] 1.3 Создать `src/notifier/telegram.py`: `TelegramNotifier(AbstractNotifier)`; конструктор `(self, bot_token=None, channel_id=None, formatter=None)` с дефолтами из `src.config` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`) и вызовом `super().__init__(formatter)`
- [x] 1.4 Реализовать `TelegramNotifier.notify(message)`: при пустом токене/канале — предупреждение «⚠️ Telegram не настроен – сообщение не отправлено.» и ранний выход; иначе HTTP-блок из старого `send_signal` дословно (URL, payload `chat_id`/`text`, timeout=10, обработка не-200 и исключений через print), но с атрибутами экземпляра вместо констант модуля
- [x] 1.5 Создать `src/notifier/console.py`: `ConsoleNotifier(AbstractNotifier)`, `notify` печатает сообщение в stdout
- [x] 1.6 Наполнить `src/notifier/__init__.py`: реэкспорты (`AbstractNotifier`, `TelegramNotifier`, `ConsoleNotifier`, `DecisionFormatter`, `get_notifier`); фабрика `get_notifier()` — словарь `{"telegram": TelegramNotifier, "console": ConsoleNotifier}`, чтение `NOTIFIER` из конфига, неизвестное значение → `ValueError` с перечнем доступных каналов

## 2. Конфигурация и раннер

- [x] 2.1 Добавить в `src/config.py` константу `NOTIFIER = "telegram"` с комментарием о допустимых значениях ("telegram" | "console")
- [x] 2.2 Переключить `src/scheduler/runner.py`: импорты `format_decision`/`send_signal` → `from src.notifier import get_notifier`; до цикла `notifier = get_notifier()`; в теле стратегии `notifier.notify_decision(decision, instrument_label)`
- [x] 2.3 Убедиться, что других потребителей `format_decision`/`send_signal` нет (grep по src/ и tools/)

## 3. Удаление старого кода

- [x] 3.1 Удалить `src/notifier/formatter.py`
- [x] 3.2 Удалить `src/notifier/sender.py`
- [x] 3.3 Очистить устаревший `__pycache__` пакета при необходимости

## 4. Тесты

- [x] 4.1 Переписать `tests/unit/test_formatter.py` на `DecisionFormatter().format(...)`: все 12 существующих кейсов переносятся без изменения утверждений (импорт и способ вызова меняются)
- [x] 4.2 Создать `tests/unit/test_notifiers.py`:
  - `AbstractNotifier` нельзя инстанцировать; наследник без `notify()` нельзя создать
  - `notify_decision` доставляет отформатированный текст через фейковый наследник (фиксирует вызов `notify`)
  - подмена форматтера: переданный в конструктор форматтер используется вместо дефолтного
  - `ConsoleNotifier.notify` печатает сообщение (capsys)
  - фабрика: `"telegram"` → `TelegramNotifier`, `"console"` → `ConsoleNotifier`, неизвестное значение → `ValueError` с перечнем каналов (значение `NOTIFIER` патчить через monkeypatch на `src.notifier`)
- [x] 4.3 Тест отправки Telegram с мок-запросом (`unittest.mock.patch("requests.post")`): успешный 200 — запрос с корректным URL/payload/timeout; не-200 — запись ошибки; исключение сети — перехват без raise; незаполненный токен — предупреждение и отсутствие вызова `requests.post`
- [x] 4.4 Переписать `tests/unit/test_runner.py` (потребитель обнаружен grep'ом в задаче 2.3, в план изначально не попал): патчи `send_signal` → патч `get_notifier` с фейковым нотификатором-рекордером на реальном `AbstractNotifier` (мостик и форматирование остаются боевыми, все ассерты формата сообщений сохраняются)

## 5. Проверка

- [x] 5.1 Прогнать полный pytest — зелёный, включая snapshot-раннер
- [x] 5.2 Проверить линтером/типизацией, если настроены в проекте
- [x] 5.3 Ручная проверка локально: при `NOTIFIER="console"` сообщения печатаются; при `"telegram"` и заполненном `.env` сообщение приходит в канал (или фиксируется ошибка API/сети без падения бота)
