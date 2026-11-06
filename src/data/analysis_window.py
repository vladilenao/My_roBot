"""Окно расчёта стратегий и фильтров по глубине и покрытию (add-analysis-depth)."""


def analysis_window(cache, instrument, timeframe, frame, minimum, depth):
    """Окно расчёта: наибольший покрытый суффикс кадра не длиннее глубины.

    Покрытие суффикса монотонно (короче покрытого — тоже покрыт), поэтому
    при пробеле внутри глубины длина ищется делением пополам. Допуск
    минимума проверяет вызывающий: окно не короче ``minimum``, старый пробел
    глубже минимума сужает окно, а не блокирует расчёт (add-analysis-depth).
    """
    size = min(len(frame), depth)
    if size <= minimum or cache.coverage_for(instrument, timeframe, frame.tail(size)):
        return frame.tail(size).reset_index(drop=True).copy()
    covered, uncovered = minimum, size
    while uncovered - covered > 1:
        middle = (covered + uncovered) // 2
        if cache.coverage_for(instrument, timeframe, frame.tail(middle)):
            covered = middle
        else:
            uncovered = middle
    return frame.tail(covered).reset_index(drop=True).copy()
