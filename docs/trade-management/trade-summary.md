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
| `Initial Risk` | Original risk amount from the first reservation. |
| `Result` | Net PnL divided by Initial Risk, in R; empty for missing or zero risk. |
| `MAE` / `MFE` | Adverse/favorable observed price excursion normalized by Initial Risk. |

Internal lifecycle codes are `PLANNED`, `ENTRY_PENDING`, `OPEN`,
`PARTIALLY_CLOSED`, `CLOSED`, `CANCELLED`, `REJECTED`, and `ERROR`. They are
never written to this CSV; users see Russian labels instead.
