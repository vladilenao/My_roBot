import pandas as pd
import pandas_ta_classic as ta
import numpy as np


def tech_analyze(data):
    """
    Добавляет индикаторы MACD, RSI, Stoch и формирует сигналы.
    """

    data_ta = data.copy()

    data_ta.ta.macd(append=True, colprefix='macd', fast=12, slow=26, signal=9, close='close')
    data_ta.ta.stoch(append=True, colprefix='stoch', k=14, d=3, smooth_k=3, close='close')
    data_ta['rsi'] = ta.rsi(data['close'])

    data_ta.columns = data_ta.columns.str.lower()

    # RSI сигнал
    data_ta['rsi_signal'] = np.where(
        ((data_ta['rsi'] > 50) & (data_ta['rsi'].shift(1) < 50)), 1,
        np.where((data_ta['rsi'] < 50) & (data_ta['rsi'].shift(1) > 50), -1, 0)
    )

    # MACD сигнал
    data_ta['macd_signal'] = np.where(
        (data_ta['macds_12_26_9'] > data_ta['macds_12_26_9'].shift(1)) &
        (data_ta['macds_12_26_9'] > data_ta['macd_12_26_9'].shift(1)) &
        (data_ta['macd_12_26_9'] < 0) & (data_ta['macds_12_26_9'] < 0), 1,
        np.where(
            (data_ta['macds_12_26_9'] < data_ta['macds_12_26_9'].shift(1)) &
            (data_ta['macds_12_26_9'] < data_ta['macd_12_26_9'].shift(1)) &
            (data_ta['macd_12_26_9'] > 0) & (data_ta['macds_12_26_9'] > 0), -1, 0
        )
    )

    # Stoch сигнал
    data_ta['stoch_signal'] = np.where(
        (data_ta['stochk_14_3_3'] > data_ta['stochk_14_3_3'].shift(1)) &
        (data_ta['stochk_14_3_3'].shift(1) < 20) &
        (data_ta['stochk_14_3_3'] > 20) & (data_ta['stochk_14_3_3'] < 50), 1,
        np.where(
            (data_ta['stochk_14_3_3'] < data_ta['stochk_14_3_3'].shift(1)) &
            (data_ta['stochk_14_3_3'].shift(1) > 80) &
            (data_ta['stochk_14_3_3'] < 80) & (data_ta['stochk_14_3_3'] > 50), -1, 0
        )
    )

    return data_ta
