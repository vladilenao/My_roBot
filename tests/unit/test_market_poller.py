from __future__ import annotations

import threading
from unittest.mock import MagicMock

from src.data.poller import LiveMarketDataPoller


def test_live_poller_refreshes_data_in_background():
    cache = MagicMock()
    first_call = threading.Event()
    cache.refresh_if_new_candle.side_effect = lambda *args, **kwargs: first_call.set()
    poller = LiveMarketDataPoller(cache, ["5m", "1m"], poll_seconds=0.01)

    poller.start()
    assert first_call.wait(1)
    poller.close()

    assert cache.refresh_if_new_candle.call_args.kwargs["force"] is True
    assert cache.refresh_if_new_candle.call_args.args[0] in {"1m", "5m"}
