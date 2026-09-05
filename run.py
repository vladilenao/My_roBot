from src.market_context import (
    MarketContextCache,
    SRLevelsCalculator,
    TrendAnalyzer,
)
from src.decision import RiskManager, SignalFilter
from src.bot import TradingBot
from src.config import (
    FUTURE_STRATEGIES,
    HEARTBEAT_EVERY_TICKS,
    SHARE_STRATEGIES,
    SLEEP_SECONDS,
    TICKER,
    TICK_POLL_SECS,
    TICK_TIMEOUT_SECS,
    TIMEFRAME,
    TINKOFF_TOKEN,
    INSTRUMENT_TYPE,
)
from src.data.cache import MarketDataCache
from src.data.loader import load_candles
from src.execution import NotifyOnlyExecutionPort
from src.instruments.selector import select_instruments
from src.notifier import get_notifier
from src.scheduler.timing import CandleScheduler


def main():
    instruments = select_instruments() or [(TICKER, TICKER, INSTRUMENT_TYPE)]
    notifier = get_notifier()
    timeline = CandleScheduler(timeframe=TIMEFRAME, sleep_secs=SLEEP_SECONDS)
    data_cache = MarketDataCache(
        loader=load_candles, timeline=timeline, token=TINKOFF_TOKEN
    )

    TradingBot(
        instruments=instruments,
        notifier=notifier,
        strategy_map=_strategy_map(),
        data_cache=data_cache,
        timeline=timeline,
        execution=NotifyOnlyExecutionPort(notifier),
        share_strategies=SHARE_STRATEGIES,
        future_strategies=FUTURE_STRATEGIES,
        heartbeat_every_ticks=HEARTBEAT_EVERY_TICKS,
        tick_poll_secs=TICK_POLL_SECS,
        tick_timeout_secs=TICK_TIMEOUT_SECS,
        context_cache=MarketContextCache(
            data_cache=data_cache,
            trend_analyzer=TrendAnalyzer(),
            sr_calculator=SRLevelsCalculator(),
        ),
        signal_filter=SignalFilter(),
        risk_manager=RiskManager(),
    ).run()


def _strategy_map():
    from src.strategies.macd_rsi_stoch import DEFAULT_CONFIG as MACD
    from src.strategies.flat_triangle import DEFAULT_CONFIG as FLAT

    return {
        "macd_rsi_stoch": MACD,
        "flat_triangle": FLAT,
    }


if __name__ == "__main__":
    main()
