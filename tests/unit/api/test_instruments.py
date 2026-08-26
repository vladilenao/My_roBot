from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from t_tech.invest import CandleInterval, InstrumentStatus

from src.api.instruments import find_working_instrument


def make_response(*tickers_and_uids):
    return SimpleNamespace(
        instruments=[SimpleNamespace(ticker=t, uid=u) for t, u in tickers_and_uids]
    )


@pytest.fixture
def setup(monkeypatch):
    client = MagicMock()
    calls = []

    def install(instruments=(), candles=None, candles_error=False):
        response = make_response(*instruments)

        def fake_retry(fn, *args, **kwargs):
            calls.append((fn, kwargs))
            if fn is client.get_all_candles:
                if candles_error:
                    raise RuntimeError("candles api down")
                return list(candles or [])
            return response

        monkeypatch.setattr("src.api.instruments.api_call_with_retry", fake_retry)
        fake_retry.calls = calls
        return fake_retry

    def install_custom(fake_retry):
        monkeypatch.setattr("src.api.instruments.api_call_with_retry", fake_retry)

    return SimpleNamespace(client=client, calls=calls, install=install, install_custom=install_custom)


class TestEndpointDispatch:
    def test_share_uses_shares_endpoint(self, setup):
        retry = setup.install(instruments=[("SBER", "uid-1")], candles=[object()])

        uid = find_working_instrument(setup.client, "SBER", "share")

        assert uid == "uid-1"
        assert retry.calls[0][0] is setup.client.instruments.shares

    def test_future_uses_futures_endpoint(self, setup):
        retry = setup.install(instruments=[("NGU6", "uid-1")], candles=[object()])

        assert find_working_instrument(setup.client, "NGU6", "future") == "uid-1"
        assert retry.calls[0][0] is setup.client.instruments.futures

    def test_etf_uses_etfs_endpoint(self, setup):
        retry = setup.install(instruments=[("TGLD", "uid-1")], candles=[object()])

        assert find_working_instrument(setup.client, "TGLD", "etf") == "uid-1"
        assert retry.calls[0][0] is setup.client.instruments.etfs

    def test_currency_uses_currencies_endpoint(self, setup):
        retry = setup.install(instruments=[("USD000UTSTOM", "uid-1")], candles=[object()])

        assert find_working_instrument(setup.client, "USD000UTSTOM", "currency") == "uid-1"
        assert retry.calls[0][0] is setup.client.instruments.currencies

    def test_unknown_type_raises_without_api_call(self, setup):
        retry = setup.install()

        with pytest.raises(ValueError, match="Неподдерживаемый тип инструмента: bond"):
            find_working_instrument(setup.client, "XYZ", "bond")

        assert not retry.calls


class TestValidationFlow:
    def test_test_candles_use_day_interval(self, setup):
        retry = setup.install(instruments=[("SBER", "uid-1")], candles=[object()])

        find_working_instrument(setup.client, "SBER")

        candle_kwargs = retry.calls[1][1]
        assert candle_kwargs["interval"] == CandleInterval.CANDLE_INTERVAL_DAY
        assert candle_kwargs["instrument_id"] == "uid-1"

    def test_instrument_status_requested_as_base(self, setup):
        retry = setup.install(instruments=[("SBER", "uid-1")], candles=[object()])

        find_working_instrument(setup.client, "SBER")

        assert retry.calls[0][1]["instrument_status"] == InstrumentStatus.INSTRUMENT_STATUS_BASE


class TestCandidateLoop:
    def test_returns_first_match_with_candles(self, setup):
        setup.install(
            instruments=[("SBER", "uid-a"), ("SBER", "uid-b")],
            candles=[object()],
        )

        assert find_working_instrument(setup.client, "SBER") == "uid-a"

    def test_skips_match_with_empty_candles(self, setup):
        candle_calls = {"n": 0}

        def fake_retry(fn, *args, **kwargs):
            setup.calls.append((fn, kwargs))
            if fn is setup.client.get_all_candles:
                candle_calls["n"] += 1
                return [] if candle_calls["n"] == 1 else [object()]
            return make_response(("SBER", "uid-a"), ("SBER", "uid-b"))

        setup.install_custom(fake_retry)

        assert find_working_instrument(setup.client, "SBER") == "uid-b"

    def test_continues_past_candle_request_failure(self, setup):
        candle_calls = {"n": 0}

        def fake_retry(fn, *args, **kwargs):
            setup.calls.append((fn, kwargs))
            if fn is setup.client.get_all_candles:
                candle_calls["n"] += 1
                if candle_calls["n"] == 1:
                    raise RuntimeError("boom")
                return [object()]
            return make_response(("SBER", "uid-a"), ("SBER", "uid-b"))

        setup.install_custom(fake_retry)

        assert find_working_instrument(setup.client, "SBER") == "uid-b"

    def test_ticker_not_found_raises(self, setup):
        setup.install(instruments=[("GAZP", "uid-1")], candles=[object()])

        with pytest.raises(ValueError, match="не найден или недоступен"):
            find_working_instrument(setup.client, "SBER", "share")

    def test_all_matches_fail_raises(self, setup):
        setup.install(instruments=[("SBER", "uid-a"), ("SBER", "uid-b")], candles=[])

        with pytest.raises(ValueError, match="не найден или недоступен"):
            find_working_instrument(setup.client, "SBER", "share")
