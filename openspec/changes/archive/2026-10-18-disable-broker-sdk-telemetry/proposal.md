## Why

Каждый полный прогон `pytest` заканчивался строкой «Sentry is attempting to send 1 pending events». Источник — SDK брокера `t-tech-investments` 1.49.3: `Client.__enter__` вызывает `init_error_hub`, а тот включает `sentry_sdk` на весь процесс со встроенным адресом T-Bank. Выключателя в SDK нет.

В тестах настоящий `Client` создавали три теста `tests/unit/data/test_loader.py::TestIterationRetry`: они подменяли `src.data.loader.Client`, хотя загрузчик давно создаёт клиента через `src.api.client.TinkoffClientProvider`. Тест получал токен из `.env` и канал к боевому адресу брокера (RPC не было только потому, что подменены поиск инструмента и вызов API). После этого интеграция Sentry с `logging` собирала ERROR-записи следующих тестов (6 штук). Фильтр SDK их отбрасывал, но Sentry отправлял в T-Bank служебный `client_report` «отброшено 6 ошибок».

В боевом роботе то же происходит на каждом клиенте: сбои потоков `UNAVAILABLE` уходят в Sentry T-Bank со служебными полями SDK и тремя последними записями нашего лога (breadcrumbs). Breadcrumbs собираются из записей `logging` до нашего форматтера, поэтому маскирование `[скрыто]` к ним не применяется — это расходится с правилом о секретах в `AGENTS.md`.

## What Changes

- **Телеметрия SDK брокера отключена.** Импорт `src.api.client` заменяет `init_error_hub` и `async_init_error_hub` в `t_tech.invest.clients` пустыми функциями; если SDK изменился и точек нет, в лог пишется предупреждение.
- **Тесты загрузчика** подменяют `src.api.client.Client` — тот класс, через который клиент создаётся на самом деле; устаревший импорт `Client` в `src/data/loader.py` удалён.
- **Защита в `tests/conftest.py`:** создание настоящих `Client`/`AsyncClient` SDK и `sentry_sdk.init` в тестах завершают тест с понятной причиной. Тест, которому нужен настоящий клиент SDK без сети и токена, помечается `real_broker_sdk`; запрет `sentry_sdk.init` для него остаётся.

## Capabilities

### New Capabilities

Нет.

### Modified Capabilities

- `api-client`: телеметрия SDK брокера отключена.
- `test-layout`: тесты не создают настоящий клиент брокера и не включают Sentry.
