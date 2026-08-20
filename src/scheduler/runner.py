import time
import warnings
from src.config import (
    TINKOFF_TOKEN, TIMEFRAME, TICKER, INSTRUMENT_TYPE,
    SIGNAL_WINDOW, SLEEP_SECONDS
)
from src.data.loader import load_candles
from src.indicators.calculator import tech_analyze
from src.signals.generator import get_last_signals, make_decision
from src.notifier.sender import send_signal

warnings.filterwarnings('ignore')


def run_bot(instruments=None):
    """Бесконечный цикл – проверяет сигналы и отправляет уведомления."""

    if instruments is None:
        instruments = [(TICKER, INSTRUMENT_TYPE)]

    print("Бот запущен. Для остановки нажмите Ctrl+C.")

    while True:
        try:
            for ticker, instrument_type in instruments:
                instrument_label = f"{ticker} {instrument_type}"

                df, instrument_id = load_candles(
                    ticker=ticker,
                    instrument_type=instrument_type,
                    timeframe=TIMEFRAME,
                    start_date=None,
                    end_date=None,
                    token=TINKOFF_TOKEN
                )

                if df.empty:
                    print(f"Нет данных для {ticker} - пропускаем.")
                    continue

                data_ta = tech_analyze(df)
                macd_sum, rsi_sum, stoch_sum = get_last_signals(data_ta, window=SIGNAL_WINDOW)
                current_price = data_ta['close'].iloc[-1]
                decision_text = make_decision(macd_sum, rsi_sum, stoch_sum, current_price, instrument_label)
                message = f"[{instrument_label}] Сигналы: MACD={macd_sum}, RSI={rsi_sum}, Stoch={stoch_sum}\n{decision_text}"
                send_signal(message)

            time.sleep(SLEEP_SECONDS)

        except KeyboardInterrupt:
            print("\nБот остановлен.")
            break
        except Exception as e:
            print(f"Ошибка в цикле: {e}")
            time.sleep(SLEEP_SECONDS)
