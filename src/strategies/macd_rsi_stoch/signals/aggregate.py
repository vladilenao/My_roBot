def get_last_signals(data_ta, window=5):
    """
    Возвращает сумму сигналов за последние `window` свечей.
    """

    if len(data_ta) < window:
        window = len(data_ta)

    macd_sum = data_ta['macd_signal'].iloc[-window:].sum()
    rsi_sum = data_ta['rsi_signal'].iloc[-window:].sum()
    stoch_sum = data_ta['stoch_signal'].iloc[-window:].sum()
    return macd_sum, rsi_sum, stoch_sum
