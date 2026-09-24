## 1. Таймфрейм сделки в плане

- [x] 1.1 Добавить поле `timeframe: str = ""` в `TradePlan` (`src/trade_management/models.py`, frozen dataclass)
- [x] 1.2 Сохранять таймфрейм в `_plan_payload` (`src/trade_management/manager.py`, ключ `"timeframe"`)
- [x] 1.3 Заполнять таймфрейм в `_plan_entry` через `dataclasses.replace(plan, timeframe=timeframe or "")` (`src/trade_management/manager.py`)
- [x] 1.4 Восстанавливать таймфрейм в `storage._load_trade` через `plan_data.get("timeframe", "")` (`src/trade_journal/storage.py`)
- [x] 1.5 Юнит-тест: `plan_json` сохраняет и восстанавливает `timeframe`; план без ключа восстанавливается с `""` (`tests/unit/trade_management/test_state.py`; storage-кейс — `tests/unit/trade_journal/test_trade_journal.py`)

## 2. TTL-отмена незаполненного входа

- [x] 2.1 Добавить в `RecoveredTrade` поле `entry_ack_at: datetime | None` и заполнять его в `_load_trade` по OPEN-ордеру со статусом `ACK` (`SELECT updated_at FROM orders WHERE trade_id=? AND action_type='OPEN' ORDER BY created_at LIMIT 1`); статус не `ACK`/наивное/непарсимое время → `None`
- [x] 2.2 Добавить хелпер `_entry_ttl_seconds(timeframe) -> int` в `manager.py` (пустой/≥240м → 86400; ≤30м → 3600; <240м → 14400; через `src.scheduler.timing.tf_period_minutes`)
- [x] 2.3 Добавить метод `_cancel_stale_entries(ticker, now)` в `TradeManager`: кандидат = `ENTRY_PENDING` c `entry_ack_at` старше `now - TTL`; отправка `CancelEntry(reason="entry-timeout")` через `submit_action`
- [x] 2.4 Вызывать `_cancel_stale_entries` из `manage()` после ветки `_expires_within` (аналог `_close_expiring`), принимая параметр `now`
- [x] 2.5 Юнит-тест в `tests/unit/trade_management/test_manager.py`: просроченный ACK-вход отменяется с `entry-timeout`, свежий — нет; сделка без ACK (не отправленная) не отменяется; чужие инструменты не трогаются; после диспатча фаза `CANCELLED`
- [x] 2.6 Юнит-тест: сирота-кейс (старый план без таймфрейма → TTL 24 ч, ACK старше срока) переходит в `CANCELLED` без специального кода

## 3. Русские причины в пользовательских CSV

- [x] 3.1 Словарь `REASON_LABELS` и хелпер `reason_label(code)` в `src/trade_journal/export.py` (коды `next-bar`, `entry-timeout`, `contract-expiring`, `profile-entry`, `protective`, `tp`, `risk-cap`, `duplicate-signal`, `opposite-exposure`, …; неизвестный код возвращается как есть)
- [x] 3.2 Применить `reason_label` к «Причине» строки ленты (`export.py`, журнальная строка)
- [x] 3.3 Применить `reason_label` к «Финальная причина» карточки (`export.py`, `_summary_row`)
- [x] 3.4 Тест: известный код в ленте и карточке выводится русской фразой; неизвестный — исходным кодом
  (отклонение: тесты в `tests/integration/test_trade_journal_export.py`, т.к. там реальный харнесс CSV; файл из задумки `tests/unit/trade_journal/test_trade_journal.py` — legacy-журнал)

## 4. Верификация

- [x] 4.1 Прогнать `./.venv/bin/python -m pytest -q` (933 passed; ruff отсутствует в .venv — гоняется через системный `ruff` из /opt/anaconda3/bin)
- [x] 4.2 Прогнать `./.venv/bin/python -m ruff check .` (27 ошибок — только предсуществующие в `tools/` и `openspec/_check_sync.py`, допускаемые по AGENTS.md; `ruff check src tests` — чисто)
- [x] 4.3 Проверить на текущей базе: сирота 18.09 (tf `""` → TTL 24 ч, ack 18.09 14:00:14+00:00, возраст 87 ч) — `stale=True` на read-only проверке; живой диспатч не запускался