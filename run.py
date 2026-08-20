from src.scheduler.runner import run_bot
from src.instruments.selector import select_instruments

if __name__ == "__main__":
    instruments = select_instruments()
    run_bot(instruments)