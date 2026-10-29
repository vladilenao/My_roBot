## 1. Боевой клиент

- [x] 1.1 `disable_broker_telemetry()` в `src/api/client.py`, вызов при импорте модуля; предупреждение, если точек SDK нет.
- [x] 1.2 Тесты: точки заменены; замены не включают Sentry; вход в настоящие `Client`/`AsyncClient` без сети не активирует Sentry; отсутствие точек даёт предупреждение; повторный вызов безопасен.

## 2. Тесты

- [x] 2.1 `TestIterationRetry` подменяет `src.api.client.Client`; удалён устаревший импорт в `src/data/loader.py`.
- [x] 2.2 Автоматическая защита в `tests/conftest.py` и маркер `real_broker_sdk`; тесты самой защиты.
- [x] 2.3 Полный `pytest` без строки «Sentry is attempting to send».
