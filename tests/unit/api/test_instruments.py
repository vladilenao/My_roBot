from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from t_tech.invest import CandleInterval, InstrumentStatus

from src.api.instruments import (
    find_working_instrument,
    find_working_instrument_with_name,
    instrument_short_name,
    load_share_contracts,
)


def make_response(*tickers_and_uids, names=None):
    return SimpleNamespace(
        instruments=[
            SimpleNamespace(ticker=t, uid=u, name=(names or {}).get(t, ""))
            for t, u in tickers_and_uids
        ]
    )


@pytest.fixture
def setup(monkeypatch):
    client = MagicMock()
    calls = []

    def install(instruments=(), candles=None, candles_error=False, names=None):
        response = make_response(*instruments, names=names)

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


class TestShortNames:
    def test_share_is_named_by_its_ticker(self):
        inst = SimpleNamespace(ticker="SBER", name="Сбербанк России ПАО ао")

        assert instrument_short_name(inst, "share") == "SBER"

    def test_contract_takes_first_word_of_name(self):
        inst = SimpleNamespace(ticker="NGU6", name="NG-9.26 NG")

        assert instrument_short_name(inst, "future") == "NG-9.26"

    def test_contract_without_name_is_unknown(self):
        inst = SimpleNamespace(ticker="NGU6", name="")

        assert instrument_short_name(inst, "future") is None

    def test_uid_and_name_come_from_one_catalog_call(self, setup):
        retry = setup.install(
            instruments=[("NGU6", "uid-1")], candles=[object()], names={"NGU6": "NG-9.26 NG"}
        )

        assert find_working_instrument_with_name(setup.client, "NGU6", "future") == (
            "uid-1",
            "NG-9.26",
        )
        assert [fn for fn, _ in retry.calls].count(setup.client.instruments.futures) == 1

    def test_unknown_name_is_none_not_ticker(self, setup):
        setup.install(instruments=[("NGU6", "uid-1")], candles=[object()], names={"NGU6": ""})

        assert find_working_instrument_with_name(setup.client, "NGU6", "future") == ("uid-1", None)


class TestLoadShareContracts:
    def _install(self, monkeypatch, shares):
        client = MagicMock()
        response = SimpleNamespace(instruments=shares)
        calls = []

        def fake_retry(fn, **kw):
            calls.append(fn)
            return response

        monkeypatch.setattr("src.api.instruments.api_call_with_retry", fake_retry)
        return SimpleNamespace(client=client, calls=calls)

    def _share(self, ticker, step, lot, units=0, nano=0):
        return SimpleNamespace(
            ticker=ticker,
            min_price_increment=SimpleNamespace(units=units, nano=nano),
            lot=lot,
        )

    def test_step_cost_is_step_times_lot(self, monkeypatch):
        env = self._install(monkeypatch, [self._share("SBER", 0.01, 100, 0, 10_000_000)])

        contracts = load_share_contracts(env.client, tickers={"SBER"})

        assert contracts["SBER"].price_step == 0.01
        assert contracts["SBER"].step_cost == 1.0
        assert contracts["SBER"].go_buy == 0.0
        assert contracts["SBER"].go_sell == 0.0
        assert contracts["SBER"].expiration_date is None

    def test_lot_price_is_derived_value_per_point(self, monkeypatch):
        """`step_cost / price_step` — цена одного лота: на ней считаются объём и P&L."""
        env = self._install(monkeypatch, [self._share("SBER", 0.01, 100, 0, 10_000_000)])

        meta = load_share_contracts(env.client, tickers={"SBER"})["SBER"]

        assert meta.step_cost / meta.price_step == 100

    def test_ticker_filter_is_applied(self, monkeypatch):
        env = self._install(
            monkeypatch,
            [self._share("SBER", 0.01, 100, 0, 10_000_000), self._share("GAZP", 0.01, 10, 0, 10_000_000)],
        )

        contracts = load_share_contracts(env.client, tickers={"SBER"})

        assert set(contracts) == {"SBER"}

    def test_share_without_lot_is_skipped(self, monkeypatch):
        env = self._install(monkeypatch, [self._share("SBER", 0.01, 0, 0, 10_000_000)])

        assert load_share_contracts(env.client, tickers={"SBER"}) == {}

    def test_share_without_price_step_is_skipped(self, monkeypatch):
        env = self._install(monkeypatch, [self._share("SBER", 0.0, 100, 0, 0)])

        assert load_share_contracts(env.client, tickers={"SBER"}) == {}

    def test_shares_endpoint_is_used(self, monkeypatch):
        env = self._install(monkeypatch, [self._share("SBER", 0.01, 100, 0, 10_000_000)])

        load_share_contracts(env.client, tickers={"SBER"})

        assert env.calls == [env.client.instruments.shares]
