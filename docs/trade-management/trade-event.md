# Trade Event CSV

`trade_event.csv` is the human-readable chronological event projection. Its
technical linkage to `trade_summary.csv` is kept in SQLite by `trade_id`; no
technical ID is written to the user-facing file.

| Header | Meaning and source |
| --- | --- |
| `Время (МСК)` | Event timestamp converted from the stored UTC timestamp. |
| `Контракт` | Short contract name, never the raw ticker. |
| `Направление` | Human-readable direction from the trade side. |
| `Событие` | Russian event/action description. |
| `Количество` | Fill quantity, or the requested quantity for a non-fill order event. |
| `Цена` | Fill price, event price, or requested price when no fill exists. |
| `Объем позиции до` / `Объем позиции после` | Open quantity from replaying confirmed fills before and after the event. |
| `Средняя цена до` / `Средняя цена после` | Weighted entry price from the same replay state. |
| `Причина` | Human-readable reason supplied by the event. |

`trade_id`, `event_id`, `order_id`, and other technical identifiers remain
available in SQLite for grouping and audit, but are intentionally absent from
both headers and values in this CSV.
