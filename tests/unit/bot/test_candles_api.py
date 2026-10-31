"""API свечей для графиков UI (add-candles-api): ряды, окно, имена, ошибки, контракт."""

import json

import pytest

from src.bot.launch_control import LaunchControl
from src.data.export_api import MarketDataExportServer
from src.data.market_store import SOURCE_TBANK, MarketDataStore
from tests.unit.bot.test_launch_control import CONTROL_KEY, EXPORT_KEY, _call, _problem
from tests.unit.clusters.test_report import _check
from tests.unit.data.test_candle_reads import _candle

HIDDEN = ("NGZ6", "NGX6", "uid-")


def _roster(uid, interval, ticker, kind, short, origin="selected"):
    return {"instrument_uid": uid, "interval": interval, "ticker": ticker, "instrument_type": kind,
            "short_name": short, "origin": origin}


@pytest.fixture
def store(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.save_roster(source=SOURCE_TBANK, entries=[
        _roster("uid-ng12", "15m", "NGZ6", "future", "NG-12.26"),
        _roster("uid-ng12", "1m", "NGZ6", "future", "NG-12.26"),
        _roster("uid-ng11", "15m", "NGX6", "future", "NG-11.26", "obligation"),
        _roster("uid-sber", "15m", "SBER", "share", None),
    ])
    store.mark_roster_previous(source=SOURCE_TBANK, pairs=[("uid-ng11", "15m")])
    store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="15m",
                         candles=[_candle(15 * index, 3.40 + index / 1000) for index in range(10)])
    store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-sber", interval="15m",
                         candles=[_candle(0, 284.5), _candle(15, 284.6, complete=False)])
    store.publish_pair_readiness(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="15m",
                                 status={"state": "READY"})
    store.publish_pair_readiness(source=SOURCE_TBANK, instrument_uid="uid-ng11", interval="15m",
                                 status={"state": "GAP_HOLD"})
    return store


def _server(store):
    srv = MarketDataExportServer(store, EXPORT_KEY, port=0, control=LaunchControl(reply_timeout=2),
                                 control_token=CONTROL_KEY)
    try:
        srv.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    return srv


@pytest.fixture
def server(store):
    srv = _server(store)
    yield srv
    srv.close()


def _get(srv, path, key=CONTROL_KEY, host=None):
    status, payload, headers = _call(srv, "GET", path, key, host=host)
    assert not any(word in json.dumps(payload) for word in HIDDEN), payload
    return status, payload, headers


class TestSeries:
    def test_all_series_sorted_with_status(self, server):
        status, payload, _ = _get(server, "/api/v1/candles")
        assert status == 200
        _check(payload, {"$ref": "#/components/schemas/CandleSeriesList"})
        assert [(item["instrument"], item["timeframe"], item["status"], item["readiness"]) for item in payload["items"]] == [
            ("NG-11.26", "15m", "retired", None), ("NG-12.26", "1m", "active", None),
            ("NG-12.26", "15m", "active", "READY"), ("SBER", "15m", "active", None)]
        ng = payload["items"][2]
        assert (ng["first_open_time"], ng["last_open_time"], ng["last_close"]) == (
            "2026-10-30T06:00:00Z", "2026-10-30T08:15:00Z", "3.409")
        assert ng["_links"]["self"] == {"href": "/api/v1/candles/NG-12.26/15m"}

    def test_filter_by_instrument(self, server):
        status, payload, _ = _get(server, "/api/v1/candles?instrument=ng-12.26")
        assert status == 200 and [item["timeframe"] for item in payload["items"]] == ["1m", "15m"]
        assert _get(server, "/api/v1/candles?instrument=NG")[1]["items"] == []
        assert _get(server, "/api/v1/candles?instrument=NGZ6")[1]["items"] == []


class TestWindow:
    def test_last_candles_and_earlier_link(self, server):
        status, payload, _ = _get(server, "/api/v1/candles/NG-12.26/15m?limit=3")
        assert status == 200
        _check(payload, {"$ref": "#/components/schemas/CandlePage"})
        assert payload["instrument"] == "NG-12.26" and payload["from"] is None
        assert [item["open_time"][11:16] for item in payload["candles"]] == ["07:45", "08:00", "08:15"]
        assert payload["_links"] == {
            "self": {"href": "/api/v1/candles/NG-12.26/15m?limit=3"},
            "series": {"href": "/api/v1/candles?instrument=NG-12.26"},
            "earlier": {"href": "/api/v1/candles/NG-12.26/15m?to=2026-10-30T07:45:00Z&limit=3"}}

    def test_paging_back_is_seamless(self, server):
        seen, path = [], "/api/v1/candles/NG-12.26/15m?limit=4"
        while path:
            payload = _get(server, path)[1]
            seen = [item["open_time"] for item in payload["candles"]] + seen
            path = payload["_links"].get("earlier", {}).get("href")
        assert len(seen) == 10 and seen == sorted(set(seen))

    def test_name_is_case_insensitive_and_canonical(self, server):
        status, payload, _ = _get(server, "/api/v1/candles/ng-12.26/15m")
        assert status == 200 and payload["instrument"] == "NG-12.26" and len(payload["candles"]) == 10

    def test_share_by_ticker_with_trailing_incomplete(self, server):
        payload = _get(server, "/api/v1/candles/SBER/15m")[1]
        assert payload["instrument_type"] == "share"
        assert [item["complete"] for item in payload["candles"]] == [True, False]

    def test_window_bounds(self, server):
        payload = _get(server, "/api/v1/candles/NG-12.26/15m?from=2026-10-30T09:30:00%2B03:00&to=2026-10-30T07:15:00Z")[1]
        assert payload["from"] == "2026-10-30T06:30:00Z" and payload["to"] == "2026-10-30T07:15:00Z"
        assert [item["open_time"][11:16] for item in payload["candles"]] == ["06:30", "06:45", "07:00"]


class TestNotFound:
    @pytest.mark.parametrize("path, suggestions", [
        ("/api/v1/candles/NG/15m", ["NG-12.26", "NG-11.26"]),
        ("/api/v1/candles/NGZ6/15m", ["NG-12.26"]),
        ("/api/v1/candles/NG-12.26/4h", ["1m", "15m"]),
        ("/api/v1/candles/AAAA/15m", []),
    ])
    def test_suggestions(self, server, path, suggestions):
        status, payload, headers = _get(server, path)
        _problem(status, payload, headers, "NOT_FOUND")
        assert status == 404 and payload["suggestions"] == suggestions


class TestErrors:
    @pytest.mark.parametrize("query", [
        "?from=2026-10-30T10:00:00Z&to=2026-10-30T09:00:00Z", "?from=2026-10-30T10:00:00", "?to=%D0%B2%D1%87%D0%B5%D1%80%D0%B0",
        "?limit=0", "?limit=5001", "?limit=abc", "?limit=1&limit=2",
    ])
    def test_bad_window(self, server, query):
        status, payload, headers = _get(server, "/api/v1/candles/NG-12.26/15m" + query)
        _problem(status, payload, headers, "INVALID_REQUEST")

    def test_unknown_timeframe(self, server):
        status, payload, headers = _get(server, "/api/v1/candles/NG-12.26/2m")
        _problem(status, payload, headers, "INVALID_REQUEST")

    def test_export_key_is_rejected(self, server):
        status, payload, headers = _get(server, "/api/v1/candles/NG-12.26/15m", key=EXPORT_KEY)
        _problem(status, payload, headers, "UNAUTHORIZED")

    def test_foreign_host_is_rejected(self, server):
        status, payload, headers = _get(server, "/api/v1/candles", host=f"evil.example:{server.address[1]}")
        _problem(status, payload, headers, "FORBIDDEN_HOST")

    def test_store_not_open_yet(self):
        srv = _server(None)
        try:
            for path in ("/api/v1/candles", "/api/v1/candles/NG-12.26/15m"):
                status, payload, headers = _get(srv, path)
                _problem(status, payload, headers, "MARKET_DATA_NOT_READY")
                assert status == 503
        finally:
            srv.close()


def test_home_links_to_candles(server):
    payload = _get(server, "/api/v1")[1]
    assert payload["_links"]["candles"] == {"href": "/api/v1/candles"}
    assert "candles" not in _get(server, "/api/v1", key=EXPORT_KEY)[1]["_links"]
