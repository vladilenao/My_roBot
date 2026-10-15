## 1. Маскирование

- [x] 1.1 `src/logging_setup.py`: `register_secrets`, `redact`, маскирование готовой строки в форматтере файла; форматы токена Telegram-бота и `Bearer …`.
- [x] 1.2 `setup_logging(secrets=…)`; `run.py` передаёт `TINKOFF_TOKEN`, `TELEGRAM_BOT_TOKEN`, `CLOUDFLARE_URL`, `MARKET_DATA_EXPORT_TOKEN`, `MARKET_STRUCTURE_REDIS_PASSWORD`.
- [x] 1.3 Unit-тесты сценариев требования в `tests/unit/test_logging_setup.py` на поддельных значениях.
- [x] 1.4 Проверка на текущем `data/bot_debug.log` (только чтение, вывод — счётчики): 156 вхождений секретов до маскирования, 0 после, число строк не изменилось.

## 2. Правило и завершение

- [x] 2.1 Правило о секретах в `AGENTS.md` и в контексте `openspec/config.yaml`.
- [x] 2.2 Запись в `CHANGELOG.md`.
- [x] 2.3 Полный набор `tests/`, Ruff, `openspec validate redact-secrets-in-logs --strict`.
