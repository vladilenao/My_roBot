## 1. Lifecycle And Trade Data

- [x] 1.1 Define the eight lifecycle codes and their Russian display labels, including validation of allowed transitions and separate `REJECTED` versus `CANCELLED` versus `ERROR` outcomes.
- [x] 1.2 Extend the durable trade state/events with the immutable plan values (entry, stop, TP1), initial entry quantity, add-on executions, exit reason/quantity/price sequence, and market extrema needed for MAE/MFE.
- [x] 1.3 Ensure confirmed fills update lifecycle, volume aggregates, weighted entry/exit prices, timestamps, final reason, exit scenario, gross PnL, signed fees, and net PnL atomically with the existing trade transaction.
- [x] 1.4 Define missing-data behavior for historical or incomplete trades so absent plan/extrema values remain empty and are not confused with numeric zero.

## 2. Summary Export

- [x] 2.1 Replace the current summary projection columns and Russian headers with the complete `trade_summary.csv` card contract from the trade-journal spec.
- [x] 2.2 Build each card field from one read-only SQLite snapshot, including short contract name, LONG/SHORT direction, MCK timestamps, duration, plan/fact separation, volume counts, and lifecycle label.
- [x] 2.3 Render all confirmed exits chronologically as `REASON: quantity @price`, derive the final reason and exit scenario, and calculate weighted average exit price without dropping partial exits.
- [x] 2.4 Calculate Gross PnL, signed total commission, Net PnL, Initial Risk, Result in R, MAE, and MFE with explicit handling for zero or unavailable risk.
- [x] 2.5 Preserve atomic dual-file export and ensure English lifecycle codes, raw tickers, and internal identifiers never appear in user-facing CSV output.

## 3. Verification And Documentation

- [x] 3.1 Add unit tests for lifecycle labels, valid/invalid transitions, all eight statuses, and distinction between rejected, cancelled, and errored trades.
- [x] 3.2 Add integration tests for the example trade with initial quantity, add-on, TP1 partial exit, STOP remainder, weighted prices, duration, fees, PnL, Result, MAE, and MFE.
- [x] 3.3 Add export contract tests covering every summary column, Russian values, MCK conversion, incomplete trades, historical missing fields, and atomic failure behavior.
- [x] 3.4 Update trade-journal documentation and fixtures to describe the source and calculation rule for every `trade_summary.csv` attribute.
- [x] 3.5 Run the focused trade-journal tests, the full test suite, linting, and OpenSpec validation; verify no project code is changed until the apply workflow is explicitly started.
