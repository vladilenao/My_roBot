## 1. Event Projection Contract

- [x] 1.1 Define the complete Russian header mapping for timestamp, event type, side, quantity, price, position quantities, average prices, and reason.
- [x] 1.2 Extend the event snapshot with the readable position quantity and average price before/after values while keeping `trade_id`, `event_id`, and `order_id` internal-only.
- [x] 1.3 Preserve short contract names, Russian event descriptions, and MSK conversion for every event row.
- [x] 1.4 Verify the event projection remains atomically exported together with `trade_summary.csv` from one SQLite snapshot.

## 2. Trade Linkage And Compatibility

- [x] 2.1 Replay confirmed fills in event order to calculate position state before and after each event, including adds, partial exits, and full exits.
- [x] 2.2 Keep all events internally grouped by `trade_id` and ensure their user-facing ordering can be correlated with the matching summary row without exposing technical IDs.
- [x] 2.3 Define empty-value behavior for historical events that lack a fill or state needed for before/after calculations.

## 3. Tests And Documentation

- [x] 3.1 Add an export contract test asserting the exact Russian event headers and absence of all technical ID headers and values.
- [x] 3.2 Add an integration test covering entry, add, partial take, and stop remainder with position quantity and average price before/after values.
- [x] 3.3 Add tests for MSK timestamps, short contract names, Russian event descriptions, and atomic export failure/retry behavior.
- [x] 3.4 Document the event-column mapping and the hidden linkage to `trade_summary.csv`.
- [x] 3.5 Run focused journal tests, the full test suite, linting, and OpenSpec validation.
