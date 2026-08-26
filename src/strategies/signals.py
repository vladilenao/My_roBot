from __future__ import annotations

import pandas as pd


def get_last_signals(
    data_ta: pd.DataFrame,
    window: int,
    signal_columns: list[str],
) -> list[float]:
    """Возвращает сумму сигналов за последние `window` свечей.

    Args:
        data_ta: DataFrame с сигнальными столбцами.
        window: Размер окна агрегации.
        signal_columns: Список имён сигнальных столбцов.

    Returns:
        Список сумм по каждому сигнальному столбцу.
    """
    effective_window = min(window, len(data_ta))
    return [
        data_ta[col].iloc[-effective_window:].sum() for col in signal_columns
    ]
