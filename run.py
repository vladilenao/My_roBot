from src.market_context import (
    MarketContextCache,
    SRLevelsCalculator,
    TrendAnalyzer,
)

from src import __version__
from src.decision import RiskManager, SignalFilter
from src.bot import TradingBot
from src.config import (
    ACTIVE_TIMEFRAMES,
    FUTURE_STRATEGIES,
    HEARTBEAT_EVERY_TICKS,
    SHARE_STRATEGIES,
    SLEEP_SECONDS,
    TICKER,
    TICK_POLL_SECS,
    TICK_TIMEOUT_SECS,
    TINKOFF_TOKEN,
    TRIPLE_SCREEN_PARAMS,
    INSTRUMENT_TYPE,
    LOGGING_SERVICE_UID,
    LOGGING_FILE,
    LOGGING_LEVEL,
    LOGGING_MAX_BYTES,
    LOGGING_BACKUP_COUNT,
)
from src.data.cache import MarketDataCache
from src.data.htf_provider import HtfFrameProvider
from src.data.loader import load_candles
from src.decision.filters import PROFILES
from src.decision.filters.triple_screen import TripleScreenFilter
from src.execution import NotifyOnlyExecutionPort
from src.instruments.selector import select_instruments
from src.logging_setup import get_logger, setup_logging
from src.notifier import get_notifier
from src.scheduler.timing import MultiTimeframeScheduler

log = get_logger(__name__)


def main():
    setup_logging(
        service_uid=LOGGING_SERVICE_UID,
        log_file=LOGGING_FILE,
        level=LOGGING_LEVEL,
        max_bytes=LOGGING_MAX_BYTES,
        backup_count=LOGGING_BACKUP_COUNT,
    )
    log.info("Робот v%s запущен", __version__)
    instruments = select_instruments() or [(TICKER, TICKER, INSTRUMENT_TYPE)]
    notifier = get_notifier()
    timeline = MultiTimeframeScheduler(
        timeframes=sorted(ACTIVE_TIMEFRAMES), sleep_secs=SLEEP_SECONDS
    )
    data_cache = MarketDataCache(
        loader=load_candles, timeline=timeline, token=TINKOFF_TOKEN
    )

    htf_provider = HtfFrameProvider(cache=data_cache, timeline=timeline)
    PROFILES["triple_screen"] = TripleScreenFilter(
        provider=htf_provider, params=TRIPLE_SCREEN_PARAMS
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
    from src.strategies.macd_rsi_stoch_strategy import DEFAULT_CONFIG as MACD
    from src.strategies.flat_triangle_strategy import DEFAULT_CONFIG as FLAT
    from src.strategies.harmonic_abcd_strategy import DEFAULT_CONFIG as HARMONIC
    from src.strategies.ma_cloud_rsi_macd_strategy import DEFAULT_CONFIG as MA_CLOUD

    return {
        "macd_rsi_stoch": MACD,
        "flat_triangle": FLAT,
        "harmonic_abcd": HARMONIC,
        "ma_cloud_rsi_macd": MA_CLOUD,
    }


if __name__ == "__main__":
    main()
