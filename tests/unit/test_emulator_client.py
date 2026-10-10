import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib import parse

import pandas as pd
import pytest
from t_tech.invest import CandleInterval, schemas

from src.api.client import ClientProvider
from src.api.emulator_client import (
    EmulatorClientProvider,
    EmulatorError,
    HistoricalClient,
    _quotation,
)
from src.api.instruments import find_working_instrument, load_futures_contracts
from src.trade_management.manager import _expires_within


def _client() -> HistoricalClient:
    return HistoricalClient("http://emulator:8100")


class _Http:
    """Подмена urllib-запроса: отдаёт заранее заданные ответы."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        for fragment, body in self.responses.items():
            if fragment in url:
                return _Response(body)
        raise AssertionError(f"Незапланированный запрос: {url}")

    def urls(self):
        return [url for url, _ in self.calls]


class _Response:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _http_error(status, body):
    import io
    from urllib.error import HTTPError

    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    return HTTPError("http://emulator:8100/x", status, "err", {}, io.BytesIO(payload))


def _sync_result(**overrides):
    result = {
        "producer_id": "robot-db-1",
        "target_change_id": 12,
        "after_id": 12,
        "synchronized": True,
        "snapshot_generation": 42,
    }
    result.update(overrides)
    return result


class TestMarketDataSnapshot:
    def test_preflight_post_uses_separate_timeout_and_all_candle_pages_use_snapshot(self):
        pages = _PagedHttp(_rows(5), page_size=2)
        calls = []

        def respond(url, timeout=None):
            calls.append((url, timeout))
            if hasattr(url, "get_method"):
                assert url.get_method() == "POST"
                assert url.full_url.endswith("/api/v1/market-import/sync")
                return _Response(_sync_result())
            return pages(url, timeout=timeout)

        with patch("src.api.emulator_client.request.urlopen", respond):
            provider = EmulatorClientProvider("http://emulator:8100", timeout=4, sync_timeout=30)
            assert provider.prepare_snapshot()["snapshot_generation"] == 42
            list(provider.client_context().get_all_candles(
                from_=datetime(2024, 1, 1, tzinfo=timezone.utc),
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
                instrument_id="uid-1",
            ))

        assert calls[0][1] == 30
        assert all(timeout == 4 for _, timeout in calls[1:])
        assert len(pages.requests) == 3
        assert all(page["snapshot_generation"] == "42" for page in pages.requests)

    def test_unconfirmed_response_clears_previous_snapshot(self):
        responses = iter([_sync_result(), _sync_result(synchronized=False)])
        calls = []

        def respond(url, timeout=None):
            if hasattr(url, "get_method"):
                return _Response(next(responses))
            calls.append(url)
            return _Response({"candles": []})

        with patch("src.api.emulator_client.request.urlopen", respond):
            provider = EmulatorClientProvider("http://emulator:8100")
            provider.prepare_snapshot()
            assert provider.prepare_snapshot()["synchronized"] is False
            list(provider.client_context().get_all_candles(
                from_=datetime(2024, 1, 1, tzinfo=timezone.utc),
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
                instrument_id="uid-1",
            ))

        assert "snapshot_generation" not in calls[0]

    @pytest.mark.parametrize("response", [
        _sync_result(synchronized=True, snapshot_generation=True),
        _sync_result(synchronized=True, snapshot_generation=-1),
        _sync_result(synchronized=True, after_id=11),
        _sync_result(synchronized="true"),
        _sync_result(target_change_id="12"),
    ])
    def test_malformed_sync_response_is_rejected(self, response):
        with patch("src.api.emulator_client.request.urlopen", return_value=_Response(response)):
            with pytest.raises(EmulatorError) as raised:
                EmulatorClientProvider("http://emulator:8100").prepare_snapshot()
        assert raised.value.code == "INVALID_RESPONSE"

    def test_sync_error_detail_is_readable(self):
        with patch(
            "src.api.emulator_client.request.urlopen",
            side_effect=_http_error(503, {"detail": "My Robot недоступен"}),
        ):
            with pytest.raises(EmulatorError) as raised:
                EmulatorClientProvider("http://emulator:8100").prepare_snapshot()
        assert raised.value.status == 503
        assert "My Robot недоступен" in str(raised.value)

    def test_sync_connection_reset_is_reported_as_unavailable(self):
        with patch(
            "src.api.emulator_client.request.urlopen",
            side_effect=ConnectionResetError("соединение разорвано"),
        ):
            with pytest.raises(EmulatorError) as raised:
                EmulatorClientProvider("http://emulator:8100").prepare_snapshot()
        assert raised.value.code == "SOURCE_UNAVAILABLE"


class TestQuotationConversion:
    def test_integer_price(self):
        assert _quotation(100.0) == schemas.Quotation(units=100, nano=0)

    def test_fractional_price_matches_sdk_surface(self):
        quotation = _quotation(123.456)

        assert quotation.units == 123
        assert quotation.nano == 456_000_000
        assert quotation.units + quotation.nano / 1e9 == pytest.approx(123.456)

    def test_none_is_zero(self):
        assert _quotation(None) == schemas.Quotation(units=0, nano=0)

    def test_nano_carries_into_units(self):
        assert _quotation(1.9999999999) == schemas.Quotation(units=2, nano=0)


class TestCandlesConversion:
    def _candles(self, rows):
        with patch("src.api.emulator_client.request.urlopen", _Http({"/candles": {"candles": rows}})):
            return list(
                _client().get_all_candles(
                    from_=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    to=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
                    instrument_id="uid-1",
                )
            )

    def test_row_becomes_historic_candle(self):
        candle, = self._candles(
            [{"datetime": "2024-01-01 00:00:00", "open": 100.0, "high": 101.0,
              "low": 99.0, "close": 100.5, "volume": 1000}]
        )

        assert isinstance(candle, schemas.HistoricCandle)
        assert candle.volume == 1000
        assert candle.open.units + candle.open.nano / 1e9 == pytest.approx(100.0)
        assert candle.close.units + candle.close.nano / 1e9 == pytest.approx(100.5)
        assert candle.high.units > candle.open.units
        assert candle.low.units < candle.open.units

    def test_time_is_tz_aware_utc(self):
        candle, = self._candles(
            [{"datetime": "2024-01-01 00:00:00", "open": 1.0, "high": 1.0,
              "low": 1.0, "close": 1.0, "volume": 1}]
        )

        assert candle.time.tzinfo is not None
        assert candle.time == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

    def test_empty_response_yields_nothing(self):
        assert self._candles([]) == []

    def test_query_carries_bounds_and_interval(self):
        http = _Http({"/candles": {"candles": []}})
        with patch("src.api.emulator_client.request.urlopen", http):
            list(
                _client().get_all_candles(
                    from_=datetime(2024, 1, 1, 3, 4, tzinfo=timezone.utc),
                    to=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    interval=CandleInterval.CANDLE_INTERVAL_HOUR,
                    instrument_id="uid-1",
                )
            )

        url = http.urls()[0]
        assert "instrument_id=uid-1" in url
        assert "interval=1h" in url
        assert "from=2024-01-01+03%3A04%3A00" in url
        assert "to=2024-01-02+00%3A00%3A00" in url

    def test_unsupported_interval_rejected(self):
        with pytest.raises(ValueError):
            list(
                _client().get_all_candles(
                    from_=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    interval=CandleInterval.CANDLE_INTERVAL_5_SEC,
                    instrument_id="uid-1",
                )
            )


class _PagedHttp:
    """Эмулятор `GET /api/v1/candles`: постранично, курсор, `total`.

    Повторяет контракт эмулятора: `from`/`to` включительно, `cursor` —
    исключительная нижняя граница, `next_cursor` — время последней свечи
    страницы либо `null` на последней.
    """

    def __init__(self, rows, *, page_size=2, report_total=True, repeat_cursor=False):
        self._rows = rows
        self._page_size = page_size
        self._report_total = report_total
        self._repeat_cursor = repeat_cursor
        self._last_cursor = None
        self.requests = []

    def __call__(self, url, timeout=None):
        query = dict(parse.parse_qsl(parse.urlparse(url).query))
        self.requests.append(query)
        start = 0
        if query.get("cursor"):
            start = next(
                index
                for index, row in enumerate(self._rows)
                if row["datetime"] > query["cursor"]
            )
        if query.get("from"):
            start = max(start, self._lower_bound(query["from"]))
        stop = min(start + self._page_size, len(self._rows))
        if query.get("to"):
            stop = min(stop, self._upper_bound(query["to"]))
        page = self._rows[start:stop]
        cursor = None
        if stop < len(self._rows) and page:
            cursor = page[-1]["datetime"]
            if self._repeat_cursor and self._last_cursor is not None:
                cursor = self._last_cursor
            self._last_cursor = cursor
        body = {"candles": page, "count": len(page), "next_cursor": cursor}
        if self._report_total:
            body["total"] = self._bounded_total(query)
        return _Response(body)

    def _lower_bound(self, moment):
        return next(
            (i for i, row in enumerate(self._rows) if row["datetime"] >= moment), len(self._rows)
        )

    def _upper_bound(self, moment):
        return sum(1 for row in self._rows if row["datetime"] <= moment)

    def _bounded_total(self, query):
        low = self._lower_bound(query["from"]) if query.get("from") else 0
        high = self._upper_bound(query["to"]) if query.get("to") else len(self._rows)
        return high - low

    def candle_urls(self):
        return [r for r in self.requests]


def _rows(count, start_minute=0):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "datetime": (base + timedelta(minutes=start_minute + i)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10,
        }
        for i in range(count)
    ]


class TestCandlePagination:
    def _read(self, http, **kwargs):
        with patch("src.api.emulator_client.request.urlopen", http):
            return list(
                _client().get_all_candles(
                    from_=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    to=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
                    instrument_id="uid-1",
                    **kwargs,
                )
            )

    def test_range_read_across_pages(self):
        http = _PagedHttp(_rows(5), page_size=2)

        candles = self._read(http)

        assert len(candles) == 5
        assert len(http.requests) == 3

    def test_page_limit_is_explicit(self):
        http = _PagedHttp(_rows(2), page_size=2)

        self._read(http)

        assert http.requests[0]["limit"] == "200000"

    def test_next_page_asks_strictly_after_cursor(self):
        http = _PagedHttp(_rows(5), page_size=2)

        self._read(http)

        assert "cursor" not in http.requests[0]
        assert http.requests[1]["cursor"] == "2024-01-01 00:01:00"
        assert http.requests[2]["cursor"] == "2024-01-01 00:03:00"

    def test_boundary_candle_not_duplicated(self):
        http = _PagedHttp(_rows(5), page_size=2)

        candles = self._read(http)

        times = [candle.time for candle in candles]
        assert len(times) == len(set(times))
        assert times == sorted(times)
        assert times[0] == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        assert times[-1] == datetime(2024, 1, 1, 0, 4, tzinfo=timezone.utc)

    def test_repeated_cursor_fails_instead_of_looping(self):
        http = _PagedHttp(_rows(6), page_size=2, repeat_cursor=True)

        with pytest.raises(EmulatorError) as raised:
            self._read(http)

        assert raised.value.code == "CURSOR_LOOP"
        assert "повторяющийся курсор" in str(raised.value)

    def test_single_page_source_read_once(self):
        http = _PagedHttp(_rows(3), page_size=10)

        candles = self._read(http)

        assert len(candles) == 3
        assert len(http.requests) == 1

    def test_truncation_warns_with_expected_and_actual(self, caplog):
        http = _PagedHttp(_rows(5), page_size=2)
        http._report_total = True

        with patch.object(http, "_bounded_total", return_value=9):
            with caplog.at_level(logging.WARNING, logger="src.api.emulator_client"):
                self._read(http)

        assert "прочитано 5 свечей из 9" in caplog.text
        assert "обрезан" in caplog.text

    def test_full_read_logs_no_warning(self, caplog):
        http = _PagedHttp(_rows(5), page_size=2)

        with caplog.at_level(logging.WARNING, logger="src.api.emulator_client"):
            self._read(http)

        assert caplog.text == ""

    def test_missing_total_is_not_a_warning(self, caplog):
        http = _PagedHttp(_rows(4), page_size=2, report_total=False)

        with caplog.at_level(logging.WARNING, logger="src.api.emulator_client"):
            candles = self._read(http)

        assert len(candles) == 4
        assert caplog.text == ""

    def test_loader_retry_reopens_stream_from_last_candle(self):
        from src.data.loader import load_candles

        rows = _rows(6)
        opened = []

        class _RateLimitedOnce:
            """Отдаёт первую страницу, затем роняет поток и обслуживает переоткрытие."""

            def __init__(self):
                self.calls = 0

            def __call__(self, url, timeout=None):
                query = dict(parse.parse_qsl(parse.urlparse(url).query))
                opened.append(query.get("from"))
                self.calls += 1
                if self.calls == 1:
                    return _Response({
                        "candles": rows[:2], "count": 2, "total": 6,
                        "next_cursor": rows[1]["datetime"],
                    })
                if self.calls == 2:
                    raise EmulatorError(
                        "RESOURCE_EXHAUSTED", "rate limit: ratelimit_reset=1"
                    )
                low = 0 if not query.get("cursor") else next(
                    i for i, row in enumerate(rows) if row["datetime"] > query["cursor"]
                )
                page = rows[low:]
                return _Response({
                    "candles": page, "count": len(page), "total": 6, "next_cursor": None,
                })

        with patch("src.api.emulator_client.request.urlopen", _RateLimitedOnce()):
            with patch("src.data.loader.time.sleep"):
                frame, instrument_id = load_candles(
                    "SiH5", "future", "1m",
                    start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    end_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
                    instrument_id="uid-1",
                    client_provider=EmulatorClientProvider("http://emulator:8100"),
                )

        assert instrument_id == "uid-1"
        assert set(frame["datetime"]) == {
            pd.Timestamp(row["datetime"]) for row in rows
        }
        assert len(opened) == 3
        # Прогон переоткрыт с последней собранной свечи: граница включается
        # повторно (известное поведение, см. openspec/BACKLOG.md B-2).
        assert opened[2] == "2024-01-01 00:01:00"


class TestCatalogConversion:
    _ROW = {
        "instrument_id": "uid-1",
        "ticker": "SBER",
        "instrument_type": "share",
        "name": "SBER PAO",
        "class_code": "TQBR",
        "currency": "RUB",
        "lot": 10,
        "min_price_increment": 0.01,
    }

    def test_shares_from_emulator_catalog(self):
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({"/api/v1/instruments": [self._ROW]}),
        ):
            response = _client().instruments.shares(
                instrument_status=schemas.InstrumentStatus.INSTRUMENT_STATUS_BASE
            )

        share, = response.instruments
        assert isinstance(share, schemas.Share)
        assert (share.uid, share.figi, share.ticker) == ("uid-1", "", "SBER")
        assert share.lot == 10
        assert share.min_price_increment.units + share.min_price_increment.nano / 1e9 == pytest.approx(0.01)

    def test_futures_catalog_uses_future_schema(self):
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({"/api/v1/instruments": [{**self._ROW, "ticker": "NGZ6", "instrument_type": "future"}]}),
        ):
            response = _client().instruments.futures(
                instrument_status=schemas.InstrumentStatus.INSTRUMENT_STATUS_BASE
            )

        future, = response.instruments
        assert isinstance(future, schemas.Future)
        assert future.ticker == "NGZ6"
        assert future.min_price_increment.nano == 10_000_000

    def test_etfs_and_currencies_surfaces(self):
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({"/api/v1/instruments": [{**self._ROW, "instrument_type": "etf"}]}),
        ):
            assert _client().instruments.etfs().instruments[0].ticker == "SBER"
            assert _client().instruments.currencies().instruments[0].ticker == "SBER"

    def test_futures_margin_from_card_fields(self):
        card = {
            **self._ROW,
            "min_price_increment_amount": 0.1,
            "initial_margin_on_buy": 12345.67,
            "initial_margin_on_sell": 12345.67,
        }
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({"/api/v1/instruments/uid-1": card}),
        ):
            margin = _client().instruments.get_futures_margin(instrument_id="uid-1")

        assert margin.min_price_increment.nano == 10_000_000
        assert margin.min_price_increment_amount.nano == 100_000_000
        assert margin.initial_margin_on_buy.units == 12345
        assert margin.initial_margin_on_buy.nano == 670_000_000
        assert margin.initial_margin_on_sell.units == 12345
        assert margin.initial_margin_on_sell.nano == 670_000_000

    def test_futures_margin_degrades_when_card_fields_missing(self):
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({"/api/v1/instruments/uid-1": self._ROW}),
        ):
            margin = _client().instruments.get_futures_margin(instrument_id="uid-1")

        assert margin.min_price_increment.nano == 10_000_000
        assert margin.min_price_increment_amount.nano == 10_000_000
        assert margin.initial_margin_on_buy.units == 0
        assert margin.initial_margin_on_sell.units == 0

    def test_zero_step_cost_is_reported_as_source_gave_it(self):
        card = {**self._ROW, "min_price_increment_amount": 0.0}
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({"/api/v1/instruments/uid-1": card}),
        ):
            margin = _client().instruments.get_futures_margin(instrument_id="uid-1")

        assert margin.min_price_increment_amount.units == 0
        assert margin.min_price_increment.nano == 10_000_000


class TestContractMetaLoading:
    _FUTURE_CARD = {
        "instrument_id": "uid-1", "ticker": "SiH5", "instrument_type": "future",
        "lot": 1, "min_price_increment": 1.0, "min_price_increment_amount": 1.0,
        "initial_margin_on_buy": 12345.67, "initial_margin_on_sell": 12345.67,
        "expiration_date": "2025-06-20T00:00:00+00:00",
    }

    def _contracts(self, card, ticker="SiH5"):
        responses = {
            "/api/v1/instruments/uid-1": card,
            "/api/v1/instruments": [{**card, "instrument_id": "uid-1"}],
        }
        with patch("src.api.emulator_client.request.urlopen", _Http(responses)):
            return load_futures_contracts(_client(), tickers=[ticker])

    def test_contracts_load_from_emulator(self):
        contracts = self._contracts(self._FUTURE_CARD)

        assert list(contracts) == ["SiH5"]
        assert contracts["SiH5"].price_step == pytest.approx(1.0)
        assert contracts["SiH5"].step_cost == pytest.approx(1.0)
        assert contracts["SiH5"].go_buy == pytest.approx(12345.67)
        assert contracts["SiH5"].go_sell == pytest.approx(12345.67)
        assert not pd.isna(contracts["SiH5"].expiration_date)
        assert contracts["SiH5"].expiration_date == datetime(2025, 6, 20)

    def test_expiration_from_card_parsed_as_naive_utc(self):
        contract = self._contracts(self._FUTURE_CARD)["SiH5"]

        assert contract.expiration_date.tzinfo is None
        assert contract.expiration_date == pd.Timestamp("2025-06-20 00:00:00")

    def test_expiration_absent_never_blocks_trading(self):
        card = {k: v for k, v in self._FUTURE_CARD.items() if k != "expiration_date"}

        contract = self._contracts(card)["SiH5"]

        assert pd.isna(contract.expiration_date)
        assert _expires_within(contract, datetime(2024, 1, 1), 3) is False

    def test_null_expiration_is_absence_not_current_date(self):
        card = {**self._FUTURE_CARD, "expiration_date": None}

        contract = self._contracts(card)["SiH5"]

        assert pd.isna(contract.expiration_date)
        assert contract.expiration_date is None or pd.isna(contract.expiration_date)

    def test_unparsable_expiration_is_absence_not_current_date(self):
        card = {**self._FUTURE_CARD, "expiration_date": "20.06.2025"}

        contract = self._contracts(card)["SiH5"]

        assert pd.isna(contract.expiration_date)
        assert _expires_within(contract, datetime(2024, 1, 1), 3) is False

    def test_real_expiration_blocks_entry_near_deadline(self):
        contract = self._contracts(self._FUTURE_CARD)["SiH5"]

        assert _expires_within(contract, datetime(2025, 6, 19), 3) is True
        assert _expires_within(contract, datetime(2025, 6, 1), 3) is False

    def test_margin_limits_quantity_by_guarantee(self):
        from src.portfolio.manager import PositionManager

        contract = self._contracts(self._FUTURE_CARD)["SiH5"]
        manager = PositionManager(initial_deposit=30_000.0, max_risk_pct=2.0)

        assert manager.get_go(2, contract, "SELL") == pytest.approx(24_691.34)
        assert manager.margin_ok(2, contract, "SELL") is True
        assert manager.margin_ok(3, contract, "SELL") is False

    def test_zero_margin_does_not_limit_quantity(self):
        from src.portfolio.manager import PositionManager

        card = {
            "instrument_id": "uid-1", "ticker": "NGZ6", "instrument_type": "future",
            "lot": 1, "min_price_increment": 0.5,
        }
        contract = self._contracts(card, ticker="NGZ6")["NGZ6"]
        manager = PositionManager(initial_deposit=1.0, max_risk_pct=2.0)

        assert manager.get_go(1000, contract, "SELL") == 0.0
        assert manager.margin_ok(1000, contract, "SELL") is True

    def test_share_card_without_expiration_never_expires(self):
        from src.api.instruments import load_share_contracts

        card = {
            "instrument_id": "uid-2", "ticker": "SBER", "instrument_type": "share",
            "lot": 10, "min_price_increment": 0.01, "expiration_date": None,
        }
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({"/api/v1/instruments": [card]}),
        ):
            contracts = load_share_contracts(_client(), tickers=["SBER"])

        assert contracts["SBER"].expiration_date is None
        assert _expires_within(contracts["SBER"], datetime(2024, 1, 1), 3) is False

    def test_future_surface_has_no_sentinel_values(self):
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({"/api/v1/instruments": [{**self._FUTURE_CARD, "instrument_id": "uid-1"}]}),
        ):
            future = _client().instruments.futures().instruments[0]

        assert future.expiration_date == datetime(2025, 6, 20, tzinfo=timezone.utc)
        assert future.initial_margin_on_buy.units == 12345
        assert future.min_price_increment_amount.units == 1

    def test_contract_without_expiration_never_blocks_trading(self):
        card = {
            "instrument_id": "uid-1", "ticker": "NGZ6", "instrument_type": "future",
            "lot": 1, "min_price_increment": 0.5,
        }

        contract = self._contracts(card, ticker="NGZ6")["NGZ6"]

        assert contract.go_buy == 0.0
        assert contract.go_sell == 0.0
        assert _expires_within(contract, datetime(2024, 1, 1), 3) is False

    def test_working_instrument_found_by_ticker(self):
        with patch(
            "src.api.emulator_client.request.urlopen",
            _Http({
                "/api/v1/instruments": [{
                    "instrument_id": "uid-9", "ticker": "SBER", "instrument_type": "share",
                    "lot": 10, "min_price_increment": 0.01,
                }],
                "/candles": {"candles": [
                    {"datetime": "2024-01-01 00:00:00", "open": 1.0, "high": 1.0,
                     "low": 1.0, "close": 1.0, "volume": 1}
                ]},
            }),
        ):
            uid = find_working_instrument(
                _client(), "SBER", market_now=datetime(2024, 1, 2, tzinfo=timezone.utc)
            )

        assert uid == "uid-9"


class TestContextManagement:
    def test_provider_returns_client(self):
        provider = EmulatorClientProvider("http://emulator:9999")

        client = provider.client_context()

        assert isinstance(provider, ClientProvider)
        assert isinstance(client, HistoricalClient)
        assert client.base_url == "http://emulator:9999"
        assert client._closed is False

    def test_enter_exit_marks_closed(self):
        with _client() as client:
            assert client._closed is False

        assert client._closed is True

    def test_exit_returns_false_on_error(self):
        with pytest.raises(ValueError):
            with _client():
                raise ValueError("boom")

    def test_client_reusable_after_exit(self):
        client = _client()
        client.close()

        with client:
            assert client._closed is False

    def test_provider_needs_no_token(self):
        with patch("src.api.emulator_client.request.urlopen", _Http({"/health": {"status": "ok"}})) as http:
            with EmulatorClientProvider().client_context() as client:
                assert client.ping() == {"status": "ok"}

        assert "/health" in http.urls()[0]

    def test_timeout_is_forwarded(self):
        http = _Http({"/health": {"status": "ok"}})
        with patch("src.api.emulator_client.request.urlopen", http):
            with HistoricalClient("http://emulator:8100", timeout=12.5) as client:
                client.ping()

        assert http.calls[0][1] == 12.5


class TestSourceErrors:
    def _raises(self, error, path="/api/v1/candles"):
        with patch("src.api.emulator_client.request.urlopen", MagicMock(side_effect=error)):
            with pytest.raises(EmulatorError) as raised:
                with _client() as client:
                    list(
                        client.get_all_candles(
                            from_=datetime(2024, 1, 1, tzinfo=timezone.utc),
                            interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
                            instrument_id="uid-1",
                        )
                    )
        return raised.value

    def test_history_not_available(self):
        error = self._raises(
            _http_error(404, {"error": {"code": "HISTORY_NOT_AVAILABLE", "message": "нет данных"}})
        )

        assert error.code == "HISTORY_NOT_AVAILABLE"
        assert error.status == 404
        assert "нет данных" in str(error)

    def test_instrument_not_found(self):
        error = self._raises(
            _http_error(404, {"error": {"code": "INSTRUMENT_NOT_FOUND", "message": "нет"}})
        )

        assert error.code == "INSTRUMENT_NOT_FOUND"

    def test_invalid_time_range(self):
        error = self._raises(
            _http_error(400, {"error": {"code": "INVALID_TIME_RANGE", "message": "from >= to"}})
        )

        assert error.code == "INVALID_TIME_RANGE"

    def test_unreachable_source(self):
        from urllib.error import URLError

        error = self._raises(URLError("Connection refused"))

        assert error.code == "SOURCE_UNAVAILABLE"
        assert "недоступен" in str(error)

    def test_timeout(self):
        error = self._raises(TimeoutError("timed out"))

        assert error.code == "SOURCE_TIMEOUT"

    def test_error_body_without_json(self):
        error = self._raises(_http_error(500, b"<html>oops</html>"))

        assert error.code == "INTERNAL_ERROR"
        assert error.status == 500


class TestDecimalScaleConsistency:
    def test_quotation_survives_decimal_roundtrip(self):
        price = Decimal("1234.56")

        quotation = _quotation(price)
        restored = Decimal(quotation.units) + Decimal(quotation.nano) / Decimal(1_000_000_000)

        assert restored == price
