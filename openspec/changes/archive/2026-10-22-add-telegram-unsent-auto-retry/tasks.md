## 1. Транспорт

- [x] 1.1 `ApiResult`: поля `unsent` и `detail`; `TelegramTransport.request` классифицирует `ConnectTimeout` и `ConnectionError(MaxRetryError(NewConnectionError))` как `unsent` (D1), прочие исключения — `uncertain`.
- [x] 1.2 Лог транспорта: тип исключения и класс причины, без текста исключения (D7).
- [x] 1.3 Unit-тесты на реальных исключениях `requests` (отказ в соединении на закрытом порту, DNS, `ConnectTimeout`, `ProtocolError`, `ReadTimeout`); токен и адрес прокси не попадают в лог.

## 2. Delivery-база

- [x] 2.1 Таблица `telegram_auto_retry`, статус `deferred`; `user_version` остаётся 3 (D6).
- [x] 2.2 `defer_operation`: расписание `AUTO_RETRY_DELAYS`, окно `AUTO_RETRY_WINDOW`, переход в `failed` с причиной `auto-retry-expired` (D3); `finish_operation` пишет безопасную причину в `attempt_history.detail`.
- [x] 2.3 Выборка `deferred` операций при старте с переводом истёкших в `failed`.

## 3. Канал Telegram

- [x] 3.1 `_operation`: итог `unsent` только если все попытки `unsent` (D2); `deferred` → `_Deferred`; сохранённая операция отправляется из сохранённых материалов; подтверждённая возвращает сохранённый `message_id`; `uncertain`/`failed`/`superseded` не отправляются.
- [x] 3.2 Шлюзы сделок в `_run`: ожидание событий сделки за отложенной операцией, продолжение по расписанию, порядок (D4).
- [x] 3.3 Остановка: «парковка» ожидающих событий как `deferred` без HTTP; старт: подхват `deferred` операций до новых событий, `register_confirmed` после подтверждения (D5).
- [x] 3.4 `getChat`: после транспортного сбоя повтор не раньше чем через 60 с (D8).
- [x] 3.5 Unit-тесты: досылка после сбоя; порядок сделки и независимость других сделок; смешанный исход → `uncertain`; отложенная правка корня без повтора поста; окно истекло; перезапуск с подхватом и без досылки `uncertain`; лог без секретов.

## 4. Документация и завершение

- [x] 4.1 `docs/notification/telegram-recovery.md` и `docs/notification/telegram-cards.md`: автоматическая досылка, статус `deferred`, окно, что остаётся ручным.
- [x] 4.2 Запись в `CHANGELOG.md`.
- [x] 4.3 Полный набор `tests/`, Ruff, `openspec validate add-telegram-unsent-auto-retry --strict`.
