import time
import warnings
from src.config import (
    TINKOFF_TOKEN, TIMEFRAME, TICKER, INSTRUMENT_TYPE,
    STRATEGY_ASSIGNMENTS, SLEEP_SECONDS
)
from src.data.loader import load_candles
from src.notifier.formatter import format_decision
from src.notifier.sender import send_signal
from src.strategies import get_strategy

warnings.filterwarnings('ignore')


def run_bot(instruments=None):
    """Бесконечный цикл: для каждого инструмента обрабатывает назначенные стратегии."""

    if instruments is None:
        instruments = [(TICKER, TICKER, INSTRUMENT_TYPE)]

    print("Бот запущен. Для остановки нажмите Ctrl+C.")

    while True:
        try:
            for item in instruments:
                if len(item) == 3:
                    instrument_label, ticker, instrument_type = item
                else:
                    ticker, instrument_type = item
                    instrument_label = f"{ticker} {instrument_type}"

                strategy_names = STRATEGY_ASSIGNMENTS.get(ticker, [])
                if not strategy_names:
                    print(f"Для {instrument_label} не назначено стратегий - пропускаем.")
                    continue

                df, instrument_id = load_candles(
                    ticker=ticker,
                    instrument_type=instrument_type,
                    timeframe=TIMEFRAME,
                    start_date=None,
                    end_date=None,
                    token=TINKOFF_TOKEN
                )

                if df.empty:
                    print(f"Нет данных для {instrument_label} - пропускаем.")
                    continue

                for strategy_name in strategy_names:
                    try:
                        strategy = get_strategy(strategy_name)
                        data_ta = strategy.compute(df)
                        decision = strategy.decide(data_ta)
                        message = format_decision(decision, instrument_label)
                        send_signal(message)
                    except Exception as e:
                        print(f"Ошибка стратегии '{strategy_name}' на {instrument_label}: {e}")

            time.sleep(SLEEP_SECONDS)

        except KeyboardInterrupt:
            print("\nБот остановлен.")
            break
        except Exception as e:
            print(f"Ошибка в цикле: {e}")
            time.sleep(SLEEP_SECONDS)
