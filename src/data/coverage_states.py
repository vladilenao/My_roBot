"""Состояния покрытия ожидаемых интервалов — без зависимостей, общий словарь.

``source_absent`` — полные ответы источника за проверенный диапазон не
содержат свечи после всего расписания проверок. Это не доказательство
отсутствия сделок и не плановый перерыв, но интервал разрешён для префикса
и барьера без свечи (решение владельца 13.10.2026,
add-independent-market-data-lanes).
"""

INTERVAL_STATES = frozenset({"received", "scheduled_closed", "confirmed_no_trade", "source_absent", "pending", "unresolved"})
RESOLVED_INTERVAL_STATES = frozenset({"received", "scheduled_closed", "confirmed_no_trade", "source_absent"})
# В ленту /changes «у источника нет» уходит как прежнее unresolved с
# доказательством source_absent:…, чтобы не ломать прежних потребителей.
EXPORTED_STATE = {"source_absent": "unresolved"}
