import pandas as pd


def get_last_signals(data_ta, window=5):
    """
    Возвращает сумму сигналов за последние `window` свечей.
    """


    if len(data_ta) < window:
        window = len(data_ta)  # если данных меньше, берём все

    macd_sum = data_ta['macd_signal'].iloc[-window:].sum()
    rsi_sum = data_ta['rsi_signal'].iloc[-window:].sum()
    stoch_sum = data_ta['stoch_signal'].iloc[-window:].sum()
    return macd_sum, rsi_sum, stoch_sum


def make_decision(macd_sum, rsi_sum, stoch_sum, current_price, instrument_label=""):
    """
    По сумме сигналов принимает решение: BUY, SELL или HOLD.
    Возвращает текст для отправки.
    """
    prefix = f"[{instrument_label}] " if instrument_label else ""

    if (macd_sum > 0) and (rsi_sum > 0) and (stoch_sum > 0):
        return f"{prefix}🚀 ПОКУПАТЬ! Цена: {round(current_price, 3)}"
    elif (macd_sum < 0) and (rsi_sum < 0) and (stoch_sum < 0):
        return f"{prefix}📉 ПРОДАВАТЬ! Цена: {round(current_price, 3)}"
    else:
        return f"{prefix}😴 Отдыхаем, сигналов нет."
