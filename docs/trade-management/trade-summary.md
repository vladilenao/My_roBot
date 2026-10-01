# Trade Summary CSV

`trade_summary.csv` is a user-facing projection of SQLite trade state. It is
recreated atomically with `trade_event.csv`; it is never read to restore a
trade.

| Column | Source and calculation |
| --- | --- |
| `Trade ID` | Canonical trade identifier. |
| `Контракт` | Short contract name from instrument metadata. |
| `Направление` | `BUY` becomes `LONG`, `SELL` becomes `SHORT`. |
| `Статус` | Russian label for the internal lifecycle code. |
| `Время входа` / `Время выхода` | First entry and last exit fill, converted to MSK. |
| `Длительность` | Exit time minus entry time; empty until the trade is closed. |
| `План входа` / `План стопа` / `План TP1` | Immutable plan and first target, not execution prices. |
| `Начальный объем` | Quantity of the first confirmed entry fill. |
| `Добрано` | Quantity of all later confirmed entry fills. |
| `Макс. объем` | Maximum open quantity while replaying fills chronologically. |
| `Средняя входа` | Quantity-weighted price of all entry and add-on fills. |
| `Выходы` | Chronological `REASON: quantity @price` list. |
| `Средняя выхода` | Quantity-weighted price of all exit fills. |
| `Финальная причина` / `Сценарий выхода` | Last remainder reason and ordered partial/remainder reason sequence. |
| `Gross PnL` | Realized PnL before fees. |
| `Комиссия` | All entry, add-on, and exit fees shown as a negative expense. |
| `Net PnL` | Gross PnL plus the signed commission. |
| `Плановый риск` | Risk the plan was admitted on: `reservations.original_risk_amount`. It is what sizing was allowed to spend, not what the trade ended up risking. |
| `Initial Risk` | Risk actually carried: the stop distance from the average the position really holds, priced through the contract step. Empty when the step cost is unknown. |
| `Result` | Net PnL divided by `Initial Risk`, in R; empty for missing or zero risk. |
| `MAE` / `MFE` | Adverse/favorable excursion in R against `Initial Risk`. Empty when either side is unavailable. |

`Плановый риск` and `Initial Risk` answer different questions and are kept apart
on purpose. The planned figure is the reservation the trade was sized against, so
it stays whatever the broker later charged. The realized figure is re-measured
from the average the position actually holds: a gapped entry pays a different
price and is judged against its own stop rather than the one it was admitted on.
Dividing one by the other is what makes an unremarkable gap look like a disaster
when it was planned for, and like a free lunch when it was not.

## Fills-bounded Excursion Metrics

`MAE` and `MFE` are **fills-bounded**: they are measured only over the bars on
which something actually executed. A bar that passed far below the position but
never filled anything is not evidence that the position was ever that deep
adverse — the trade could have closed before reaching it, or the level could
have been touched between bars. Counting such bars would credit or blame the
position for prices it never saw.

Two consequences follow:

- The entry bar counts. It is the bar the position was opened on, so its range
  is the first thing the position was exposed to.
- Fills-bounded is not the same as conservative. It reports the excursion the
  executed price is known to have travelled, which is the part of the move that
  was tradeable. Wider excursions would need every bar stored in the journal.

Both figures are money, not price points. One point of `NG` is worth its own step
cost, so a price distance is meaningless without it:

```
point_value       = step_cost / price_step
Initial Risk      = |average_entry − stop_price| × point_value × max_qty
MAE / MFE         = |bar extreme − average_entry| × point_value × max_qty / Initial Risk
```

`max_qty` is the peak open quantity, so a trade that doubled in size is scored
against the risk it was actually carrying when it got there. When `price_step`
or `step_cost` is missing from the trade's snapshot the ruble figures are left
empty and `Ед. PnL` reads `RAW`: raw price points are not added up across
contracts that price a point differently. Recover the factors with
`tools/backfill_contract_factors.py` rather than guessing them.

Internal lifecycle codes are `PLANNED`, `ENTRY_PENDING`, `OPEN`,
`PARTIALLY_CLOSED`, `CLOSED`, `CANCELLED`, `REJECTED`, and `ERROR`. They are
never written to this CSV; users see Russian labels instead.
