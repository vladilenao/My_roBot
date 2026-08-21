"""
Однократное скачивание фикстур для snapshot-тестов.

Скачивает свечи из Invest API и записывает входную фикстуру candles.csv вместе
с эталоном <strategy>_expected_signals.csv в tests/snapshot/data/<case>/. Глубина
запроса равна жёсткому потолку HARD_LIMIT (300 свечей). Запускается
вручную; pytest сеть не использует.

Пример:
    python tools/download_snapshot_data.py --ticker NGU6 --instrument-type future \
        --timeframe 1h --strategy macd_rsi_stoch --case NG_1h
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import SIGNAL_WINDOW, TIMEFRAMES, TINKOFF_TOKEN
from src.data.loader import load_candles
from src.indicators.calculator import tech_analyze
from t_tech.invest.utils import now

HARD_LIMIT = 300     # неприкосновенный потолок объёма скачивания на кейс и дефолтная глубина запроса

TIMEFRAME_DURATIONS = {
    '1m': timedelta(minutes=1),
    '5m': timedelta(minutes=5),
    '15m': timedelta(minutes=15),
    '1h': timedelta(hours=1),
    '1d': timedelta(days=1),
    '1w': timedelta(weeks=1),
    '1M': timedelta(days=30),  # приближение календарного месяца
}


def build_request_dates(timeframe, depth):
    end = now()
    start = end - depth * TIMEFRAME_DURATIONS[timeframe]
    return start, end


def compute_expected_signals(df, signal_window):
    """Эталон событий: rolling-агрегация сигналов + консенсус BUY/SELL."""
    data_ta = tech_analyze(df)
    sums = data_ta[['macd_signal', 'rsi_signal', 'stoch_signal']].rolling(signal_window).sum()

    events = pd.DataFrame({
        'datetime': data_ta['datetime'],
        'price': data_ta['close'],
        'macd_sum': sums['macd_signal'],
        'rsi_sum': sums['rsi_signal'],
        'stoch_sum': sums['stoch_signal'],
    }).dropna(subset=['macd_sum', 'rsi_sum', 'stoch_sum'])

    buy = (
        (events['macd_sum'] > 0)
        & (events['rsi_sum'] > 0)
        & (events['stoch_sum'] > 0)
    )
    sell = (
        (events['macd_sum'] < 0)
        & (events['rsi_sum'] < 0)
        & (events['stoch_sum'] < 0)
    )
    events = events[buy | sell].copy()
    events['signal'] = np.where(events['macd_sum'] > 0, 'BUY', 'SELL')

    columns = ['datetime', 'signal', 'price', 'macd_sum', 'rsi_sum', 'stoch_sum']
    return events[columns].reset_index(drop=True)


def save_case(ticker, instrument_type, timeframe, strategy, case_name):
    if timeframe not in TIMEFRAMES or timeframe not in TIMEFRAME_DURATIONS:
        raise SystemExit(
            f"Неподдерживаемый таймфрейм '{timeframe}'. Доступные: {list(TIMEFRAMES.keys())}"
        )

    depth = HARD_LIMIT
    start_date, end_date = build_request_dates(timeframe, depth)

    df, instrument_id = load_candles(
        ticker=ticker,
        instrument_type=instrument_type,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        token=TINKOFF_TOKEN,
    )

    if df.empty:
        raise SystemExit("API не вернул ни одной свечи - кейс не создан.")

    if len(df) > depth:
        raise SystemExit(f"Получено {len(df)} свечей - больше лимита {depth}.")

    expected = compute_expected_signals(df, SIGNAL_WINDOW)

    case_dir = PROJECT_ROOT / 'tests' / 'snapshot' / 'data' / case_name
    case_dir.mkdir(parents=True, exist_ok=True)

    candles_path = case_dir / 'candles.csv'
    expected_path = case_dir / f'{strategy}_expected_signals.csv'

    df.to_csv(candles_path, index=False)
    expected.to_csv(expected_path, index=False)

    print(f"Инструмент: {instrument_id}")
    print(f"Глубина запроса: {depth} свечей, получено: {len(df)}")
    print(f"Консенсусных событий в эталоне: {len(expected)} (BUY={int((expected['signal'] == 'BUY').sum())}, SELL={int((expected['signal'] == 'SELL').sum())})")
    print(f"Фикстура: {candles_path}")
    print(f"Эталон:   {expected_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Скачивание фикстур для snapshot-тестов.")
    parser.add_argument('--ticker', required=True, help='Тикер инструмента (например NG)')
    parser.add_argument('--instrument-type', default='future', choices=['share', 'future', 'etf', 'currency'], help='Тип инструмента')
    parser.add_argument('--timeframe', default='1h', help='Таймфрейм из src.config.TIMEFRAMES')
    parser.add_argument('--strategy', default='macd_rsi_stoch', help='Имя стратегии (входит в имя файла эталона)')
    parser.add_argument('--case', default=None, help='Имя кейса (по умолчанию TICKER_timeframe)')
    return parser.parse_args()


def main():
    args = parse_args()
    case_name = args.case or f"{args.ticker}_{args.timeframe}"
    save_case(args.ticker, args.instrument_type, args.timeframe, args.strategy, case_name)


if __name__ == '__main__':
    main()
