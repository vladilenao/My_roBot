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


def run_bot():
    """Бесконечный цикл – проверяет сигналы и отправляет уведомления."""


print("🤖 Бот запущен. Для остановки нажмите Ctrl+C.")

while True:
    try:
    # 1. Загружаем свечи за последние 30 дней (или больше, если надо)
        df, instrument_id = load_candles(
        ticker=TICKER,
        instrument_type=INSTRUMENT_TYPE,
        timeframe=TIMEFRAME,
        start_date=None,  # можно задать конкретную дату
        end_date=None,
        token=TINKOFF_TOKEN
        )

        if df.empty:
            print("❌ Нет данных – пропускаем итерацию.")
            time.sleep(SLEEP_SECONDS)
            continue

# 2. Рассчитываем индикаторы
        data_ta = tech_analyze(df)

# 3. Получаем суммы сигналов за последние N свечей
        macd_sum, rsi_sum, stoch_sum = get_last_signals(data_ta, window=SIGNAL_WINDOW)

# 4. Текущая цена (последняя закрытая)
        current_price = data_ta['close'].iloc[-1]

# 5. Принимаем решение
        decision_text = make_decision(macd_sum, rsi_sum, stoch_sum, current_price)

# 6. Отправляем
        message = f"📊 Сигналы: MACD={macd_sum}, RSI={rsi_sum}, Stoch={stoch_sum}\n{decision_text}"
        send_signal(message)

# 7. Ждём до следующей проверки
        time.sleep(SLEEP_SECONDS)

    except KeyboardInterrupt:
        print("\n👋 Бот остановлен.")
        break
    except Exception as e:  # Логируем ошибку и продолжаем работу
        print(f"❌ Ошибка в цикле: {e}")
        time.sleep(SLEEP_SECONDS)
