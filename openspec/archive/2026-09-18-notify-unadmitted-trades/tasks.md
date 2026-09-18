## 1. Result type and rejection reasons

- [x] 1.1 Add `RejectionReason(code: str, message: str)` dataclass to `src/trade_management/models.py`
- [x] 1.2 Add `SignalAdmission(actions: tuple[TradeAction, ...], rejections: tuple[RejectionReason, ...])` dataclass to `src/trade_management/models.py` with `__iter__`/`__len__`/`__getitem__` delegating to `actions` for tuple compatibility
- [x] 1.3 Add a module-level mapping/code-to-text helper (e.g. in `src/trade_management/manager.py` or `models.py`) with Russian messages for `no-contract-metadata`, `unknown-profile`, `zero-quantity`, `duplicate-signal`, `admission-error`; fallback to the raw profile code text
- [x] 1.4 Change `TradeManager.actions_for_signal` return type to `SignalAdmission`: HOLD returns empty `SignalAdmission()`; `meta is None` returns rejection `no-contract-metadata`; exception returns rejection `admission-error` with exception text (keep `log.warning`)
- [x] 1.5 In `_plan_entry` return `SignalAdmission` with reasons: unknown profile → `unknown-profile`, `ProfileResult`/`None` plan → code from `plan.state["reason"]`, `quantity <= 0` → `zero-quantity`, `submit_plan` False → `duplicate-signal`
- [x] 1.6 Ensure `_manage_owned` and `manage` keep returning tuple of actions unchanged (admission path only changes)

## 2. Notifier and port

- [x] 2.1 Add `DecisionFormatter.format_rejection(...)` in `src/notifier/base.py` producing `⛔ <инструмент> (<таймфрейм>) HH:MM | <стратегия> [<профиль>] ➜ Сделка не допущена: <причина>`, reusing header-building logic, omitting absent time/strategy/profile/timeframe blocks
- [x] 2.2 Add `AbstractNotifier.notify_rejection(decision, instrument_label, *, reason, filter_profile="", timeframe="")` delegating to `format_rejection` then `notify`
- [x] 2.3 Add abstract `ExecutionPort.report_rejection(decision, instrument, *, reason_message, filter_profile="", timeframe="")` to `src/execution/port.py`
- [x] 2.4 Implement in `NotifyOnlyExecutionPort` → `notifier.notify_rejection` with short contract name; implement in `BrokerExecutionPort` → notify when notifier present, else return without error

## 3. Orchestration

- [x] 3.1 Update `_admit_candidate` in `src/bot/trading_bot.py` to read `.actions` and dispatch them, and for each reason in `.rejections` call `self._execution.report_rejection(...)` with candidate context (instrument, timeframe, decision, filter_profile, reason message)
- [x] 3.2 Add `report_rejection` to `RecordingExecution` in `tests/unit/bot/test_trading_bot.py` recording calls for assertions

## 4. Tests

- [x] 4.1 Update `tests/unit/trade_management/test_signal_pipeline.py` calls to handle `SignalAdmission` (iterate `.actions` or rely on tuple protocol)
- [x] 4.2 Add rejection-reason tests in `test_signal_pipeline.py`: no contract metadata → `no-contract-metadata`; duplicate signal → `duplicate-signal`; risk sizing gives `quantity <= 0` → `zero-quantity`
- [x] 4.3 Update `tests/unit/bot/test_trading_bot.py` mocks `actions_for_signal.return_value = ()` → `SignalAdmission()`; add regression test that a non-admitted candidate with a rejection reason delivers `report_rejection` with task context (instrument/timeframe/decision) and that HOLD delivers none
- [x] 4.4 Add `ExecutionPort.report_rejection` tests in `tests/unit/execution/test_port.py` for both ports: notify-only forwards to notifier with short name; broker notifies when notifier given, silent otherwise
- [x] 4.5 Add `DecisionFormatter.format_rejection` / `notify_rejection` tests in `tests/unit/notifier/test_formatter.py` and `test_notifiers.py`: marker `⛔`, full header, reason text, omission of time/strategy/profile/timeframe blocks, unchanged `●` format for normal signals

## 5. Spec sync and verification

- [x] 5.1 Update sync delta specs to main specs via openspec-sync-specs (`openspec/specs/trade-management/spec.md`, `openspec/specs/notification/spec.md`, `openspec/specs/orchestration/spec.md`)
- [x] 5.2 Run `.venv/bin/python -m pytest tests/unit -q` — all pass
- [x] 5.3 Run `.venv/bin/python -m ruff check src tests` — no new violations