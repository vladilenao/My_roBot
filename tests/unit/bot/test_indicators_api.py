"""API индикаторов стратегии привязки (add-indicators-api): режимы, окно, ошибки, кэш, нагрузка, контракт."""

import json
import logging
import sqlite3
import threading
import time

import pandas as pd
import pytest

from src.assembly.contour import _strategy_map
from src.bot import indicators_view
from src.bot.indicators_view import IndicatorsView
from src.config import TIMEFRAMES, TRADE_MANAGEMENT_PROFILES, TRIPLE_SCREEN_PARAMS
from src.data.analysis_window import analysis_window
from src.data.market_store import SOURCE_TBANK, MarketDataStore
from src.history.warmup import binding_requirements
from src.strategies import get_strategy
from src.strategies.contracts import Assignment
from tests.snapshot.helper import load_candles_fixture
from tests.unit.bot.test_candles_api import _roster, _server
from tests.unit.bot.test_launch_control import CONTROL_KEY, EXPORT_KEY, _call, _problem
from tests.unit.clusters.test_report import _check

HIDDEN = ("NGZ6", "uid-")
FUTURES = {"NG": [Assignment("future-ng-macd", "macd_rsi_stoch", "levels_rr", "basic_levels", timeframe="15m"),
                  Assignment("future-ng-ma", "ma_cloud_rsi_macd", "levels_rr", "basic_levels", timeframe="15m"),
                  Assignment("future-ng-flat", "flat_triangle", "levels_rr", "basic_levels", timeframe="15m")]}
SHARES = {"SBER": [Assignment("share-sber-abcd", "harmonic_abcd", "levels_rr", "basic_levels", timeframe="15m")]}
NG_CANDLES = 400


def _rows(frame):
    return [{"datetime": row.datetime, "open": row.open, "high": row.high, "low": row.low, "close": row.close,
             "volume": row.volume, "is_complete": True} for row in frame.itertuples()]


def _synthetic(count, start="2026-10-01 07:00", freq="15min"):
    closes = [300 + 5 * ((index * 7) % 11) / 10 + index / 20 for index in range(count)]
    return [{"datetime": stamp, "open": close - 0.1, "high": close + 0.3, "low": close - 0.3, "close": close,
             "volume": 10 + index, "is_complete": True}
            for index, (stamp, close) in enumerate(zip(pd.date_range(start, periods=count, freq=freq), closes))]


@pytest.fixture
def store(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    store.save_roster(source=SOURCE_TBANK, entries=[
        _roster("uid-ng12", "15m", "NGZ6", "future", "NG-12.26"),
        _roster("uid-ng12", "1h", "NGZ6", "future", "NG-12.26"),
        _roster("uid-sber", "15m", "SBER", "share", None),
        _roster("uid-gazp", "15m", "GAZP", "share", None),
    ])
    ng = load_candles_fixture("NG_15m").tail(NG_CANDLES)
    store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="15m", candles=_rows(ng))
    store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="1h",
                         candles=_synthetic(80, freq="1h"))
    store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-sber", interval="15m", candles=_synthetic(30))
    store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-gazp", interval="15m", candles=_synthetic(100))
    return store


def _view(depth=150):
    return IndicatorsView(share_strategies=SHARES, future_strategies=FUTURES, strategy_map=_strategy_map(),
                          profiles=TRADE_MANAGEMENT_PROFILES, triple_screen=TRIPLE_SCREEN_PARAMS,
                          ladder=tuple(TIMEFRAMES), depth=depth)


@pytest.fixture
def server(store):
    srv = _server(store)
    srv.indicators = _view()
    yield srv
    srv.close()


def _get(srv, path, key=CONTROL_KEY, host=None):
    status, payload, headers = _call(srv, "GET", path, key, host=host)
    assert not any(word in json.dumps(payload) for word in HIDDEN), payload
    return status, payload, headers


def _ok(srv, path):
    status, payload, _ = _get(srv, path)
    assert status == 200, payload
    _check(payload, {"$ref": "#/components/schemas/IndicatorPage"})
    return payload


def _series(store, uid, interval="15m"):
    candles, _ = store.candle_window(source=SOURCE_TBANK, instrument_uid=uid, interval=interval, lower=None,
                                     upper=pd.Timestamp("2030-01-01", tz="UTC"), limit=5000)
    frame = pd.DataFrame({"datetime": pd.to_datetime([item["open_time"] for item in candles], utc=True)})
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = [float(item[column]) for item in candles]
    return frame


class Covered:
    def coverage_for(self, instrument, timeframe, frame):
        return not frame.empty


def _live_row(frame, position, binding, depth=150):
    """Строка, на которой живой анализ принял бы решение: окно ``analysis_window`` и ``strategy.compute``."""
    config = _strategy_map()[binding.strategy]
    strategy = get_strategy(binding.strategy, config)
    minimum = binding_requirements(binding, strategy, TRADE_MANAGEMENT_PROFILES, TRIPLE_SCREEN_PARAMS,
                                   tuple(TIMEFRAMES))[binding.timeframe]
    window = analysis_window(Covered(), None, binding.timeframe, frame.iloc[:position + 1], minimum,
                             max(minimum, depth))
    return strategy.compute(window).iloc[-1], len(window)


def _values(payload):
    return {line["name"] + "@" + item["key"]: line["values"] for item in payload["indicators"] for line in item["lines"]}


def _columns(payload, binding):
    config = _strategy_map()[binding.strategy]
    return {line.name + "@" + indicator.chart.key: line.column
            for indicator in config.indicators for line in indicator.chart.lines}


# --- режим strategy ------------------------------------------------------------------

class TestStrategyMode:
    @pytest.mark.parametrize("binding", FUTURES["NG"], ids=lambda item: item.strategy)
    def test_points_match_live_analysis(self, server, store, binding):
        payload = _ok(server, f"/api/v1/candles/NG-12.26/15m/indicators?binding={binding.id}&limit=30")
        assert (payload["mode"], payload["depth"], payload["binding_timeframe"]) == ("strategy", 150, True)
        frame = _series(store, "uid-ng12")
        columns, values = _columns(payload, binding), _values(payload)
        assert len(payload["times"]) == 30 and set(columns) == set(values)
        for offset in range(30):
            row, used = _live_row(frame, NG_CANDLES - 30 + offset, binding)
            assert used == 150
            for name, column in columns.items():
                assert values[name][offset] == pytest.approx(float(row[column]), rel=1e-7, abs=1e-12), (name, offset)

    def test_order_and_lines_of_macd_strategy(self, server):
        payload = _ok(server, "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&limit=5")
        assert [item["title"] for item in payload["indicators"]] == ["MACD 12/26/9", "RSI 14", "Стохастик 14/3/3"]
        macd = payload["indicators"][0]
        assert (macd["pane"], macd["levels"]) == ("separate", [0])
        assert [(line["name"], line["kind"]) for line in macd["lines"]] == [
            ("macd", "line"), ("signal", "line"), ("histogram", "histogram")]
        assert all(len(line["values"]) == 5 for item in payload["indicators"] for line in item["lines"])
        assert payload["binding"] == {"id": "future-ng-macd", "strategy": "macd_rsi_stoch", "timeframe": "15m"}

    def test_ma_cloud_has_fill(self, server):
        payload = _ok(server, "/api/v1/candles/NG-12.26/15m/indicators?binding=FUTURE-NG-MA&limit=3")
        cloud = payload["indicators"][0]
        assert (cloud["key"], cloud["pane"], cloud["fill"]) == ("ma_cloud", "price", {"between": ["fast", "slow"]})
        assert payload["binding"]["id"] == "future-ng-ma"

    def test_series_shorter_than_minimum_is_null(self, server):
        """SBER: 30 свечей, минимум допуска macd_rsi_stoch — 50."""
        payload = _ok(server, "/api/v1/candles/SBER/15m/indicators?binding=future-ng-macd")
        assert len(payload["times"]) == 30
        assert all(value is None for values in _values(payload).values() for value in values)

    def test_young_series_counts_on_all_candles(self, server, store):
        """GAZP: 100 свечей, минимум 50, глубина 150 — с 50-й свечи хвост из всех свечей ряда."""
        binding = FUTURES["NG"][0]
        payload = _ok(server, "/api/v1/candles/GAZP/15m/indicators?binding=future-ng-macd")
        frame, columns, values = _series(store, "uid-gazp"), _columns(payload, binding), _values(payload)
        for name, column in columns.items():
            assert values[name][:49] == [None] * 49
            for position in (49, 75, 99):
                row, used = _live_row(frame, position, binding)
                assert used == position + 1
                assert values[name][position] == pytest.approx(float(row[column]), rel=1e-7, abs=1e-12)

    def test_depth_from_configuration(self, store):
        srv = _server(store)
        srv.indicators = _view(depth=80)
        try:
            payload = _ok(srv, "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&limit=3")
        finally:
            srv.close()
        assert payload["depth"] == 80
        row, used = _live_row(_series(store, "uid-ng12"), NG_CANDLES - 1, FUTURES["NG"][0], depth=80)
        assert used == 80
        assert _values(payload)["macd@macd"][-1] == pytest.approx(float(row["macd_12_26_9"]), rel=1e-7)

    def test_strategy_without_indicators(self, server):
        payload = _ok(server, "/api/v1/candles/NG-12.26/15m/indicators?binding=share-sber-abcd&limit=4")
        assert payload["indicators"] == [] and len(payload["times"]) == 4

    def test_values_have_eight_significant_digits(self, server):
        payload = _ok(server, "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&limit=5")
        for values in _values(payload).values():
            assert all(value is None or float(f"{value:.8g}") == value for value in values)


# --- режим continuous ----------------------------------------------------------------

def test_continuous_matches_full_series(server, store):
    binding = FUTURES["NG"][0]
    payload = _ok(server, "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&mode=continuous&limit=50")
    assert (payload["mode"], payload["depth"]) == ("continuous", None)
    full = get_strategy(binding.strategy, _strategy_map()[binding.strategy]).compute(_series(store, "uid-ng12"))
    for name, column in _columns(payload, binding).items():
        expected = full[column].iloc[-50:].tolist()
        assert _values(payload)[name] == pytest.approx(expected, rel=1e-6)
    assert payload["_links"]["self"]["href"].endswith("binding=future-ng-macd&mode=continuous&limit=50")


@pytest.mark.parametrize("path", [
    "/api/v1/candles/SBER/15m/indicators?binding=future-ng-ma&mode=continuous",
    # Листание назад до начала ряда: 12 свечей 1h, облаку MA нужно SMA-40.
    "/api/v1/candles/NG-12.26/1h/indicators?binding=future-ng-ma&mode=continuous&to=2026-10-01T18:00:00Z",
], ids=["short-series", "series-start"])
def test_continuous_shorter_than_minimum_is_null(server, path):
    payload = _ok(server, path)
    assert 0 < len(payload["times"]) <= 30
    assert all(value is None for values in _values(payload).values() for value in values)


# --- окно и листание -----------------------------------------------------------------

class TestWindow:
    def test_times_follow_candles_through_earlier(self, server):
        candles_path = "/api/v1/candles/NG-12.26/15m?limit=7"
        indicators_path = "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&mode=continuous&limit=7"
        for _ in range(3):
            candles = _get(server, candles_path)[1]
            indicators = _ok(server, indicators_path)
            assert indicators["times"] == [item["open_time"] for item in candles["candles"]]
            assert indicators["_links"]["candles"]["href"] == candles["_links"]["self"]["href"]
            candles_path = candles["_links"]["earlier"]["href"]
            indicators_path = indicators["_links"]["earlier"]["href"]
            assert "binding=future-ng-macd&mode=continuous&to=" in indicators_path

    def test_links_of_default_request(self, server):
        payload = _ok(server, "/api/v1/candles/ng-12.26/15m/indicators?binding=future-ng-macd&limit=3")
        first = payload["times"][0]
        assert payload["_links"] == {
            "self": {"href": "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&mode=strategy&limit=3"},
            "candles": {"href": "/api/v1/candles/NG-12.26/15m?limit=3"},
            "earlier": {"href": "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&mode=strategy"
                                f"&to={first}&limit=3"}}

    def test_no_earlier_at_series_start(self, server):
        payload = _ok(server, "/api/v1/candles/SBER/15m/indicators?binding=future-ng-macd")
        assert "earlier" not in payload["_links"]

    def test_incomplete_last_candle(self, server, store):
        store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-gazp", interval="15m",
                             candles=[{**_synthetic(101)[-1], "is_complete": False}])
        payload = _ok(server, "/api/v1/candles/GAZP/15m/indicators?binding=future-ng-macd&limit=3")
        candles = _get(server, "/api/v1/candles/GAZP/15m?limit=3")[1]["candles"]
        assert payload["times"] == [item["open_time"] for item in candles] and candles[-1]["complete"] is False
        assert payload["last_complete"] is False and _values(payload)["macd@macd"][-1] is not None

    def test_other_timeframe_keeps_binding_depth(self, server):
        payload = _ok(server, "/api/v1/candles/NG-12.26/1h/indicators?binding=future-ng-macd&limit=40")
        assert (payload["timeframe"], payload["binding"]["timeframe"]) == ("1h", "15m")
        assert (payload["binding_timeframe"], payload["depth"]) == (False, 150)
        # 80 свечей 1h, минимум 50: первые 9 из 40 точек окна — до минимума.
        assert _values(payload)["rsi@rsi"][:9] == [None] * 9 and _values(payload)["rsi@rsi"][9] is not None


# --- ошибки --------------------------------------------------------------------------

class TestErrors:
    @pytest.mark.parametrize("query", ["", "?binding=", "?binding=future-ng-macd&mode=fast",
                                       "?binding=future-ng-macd&limit=1001", "?binding=future-ng-macd&limit=0",
                                       "?binding=future-ng-macd&from=2026-10-30T09:00:00Z&to=2026-10-30T08:00:00Z"])
    def test_bad_request(self, server, query):
        status, payload, headers = _get(server, "/api/v1/candles/NG-12.26/15m/indicators" + query)
        _problem(status, payload, headers, "INVALID_REQUEST")

    def test_unknown_binding_suggests_instrument_bindings(self, server):
        status, payload, headers = _get(server, "/api/v1/candles/NG-12.26/15m/indicators?binding=ng-macd")
        _problem(status, payload, headers, "NOT_FOUND")
        assert payload["suggestions"] == ["future-ng-macd", "future-ng-ma", "future-ng-flat"]
        status, payload, _ = _get(server, "/api/v1/candles/GAZP/15m/indicators?binding=ng-macd")
        assert status == 404 and payload["suggestions"] == []

    def test_name_and_timeframe_like_candles(self, server):
        status, payload, headers = _get(server, "/api/v1/candles/NGZ6/15m/indicators?binding=future-ng-macd")
        _problem(status, payload, headers, "NOT_FOUND")
        assert payload["suggestions"] == ["NG-12.26"]
        status, payload, _ = _get(server, "/api/v1/candles/NG-12.26/5m/indicators?binding=future-ng-macd")
        assert status == 404 and payload["suggestions"] == ["15m", "1h"]

    def test_export_key_and_foreign_host(self, server):
        path = "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&limit=1"
        status, payload, headers = _get(server, path, key=EXPORT_KEY)
        _problem(status, payload, headers, "UNAUTHORIZED")
        assert _get(server, path, host="evil.example:8101")[0] == 403

    def test_market_not_ready(self, store):
        srv = _server(None)
        srv.indicators = _view()
        try:
            status, payload, headers = _get(srv, "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd")
        finally:
            srv.close()
        _problem(status, payload, headers, "MARKET_DATA_NOT_READY")

    def test_calculation_error_logs_only_type(self, server, monkeypatch, caplog):
        def broken(*_):
            raise ValueError("кадр: 3.091,3.095 secret-frame-data")

        monkeypatch.setattr(indicators_view, "_continuous_points", broken)
        with caplog.at_level(logging.ERROR, logger=indicators_view.log.name):
            status, payload, headers = _get(
                server, "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&mode=continuous")
        _problem(status, payload, headers, "INTERNAL")
        assert "secret-frame-data" not in json.dumps(payload) and "secret-frame-data" not in caplog.text
        assert "ValueError" in caplog.text and "future-ng-macd NG-12.26 15m" in caplog.text


# --- кэш, очередь и база -------------------------------------------------------------

def test_second_request_counts_only_new_candle(server, store):
    path = "/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&limit=20"
    first = _ok(server, path)
    view = server.indicators
    assert view.computed_points == 20
    last = pd.Timestamp(first["times"][-1]).tz_convert(None)
    store.record_candles(source=SOURCE_TBANK, instrument_uid="uid-ng12", interval="15m", candles=[
        {"datetime": last + pd.Timedelta(minutes=15), "open": 3.2, "high": 3.25, "low": 3.18, "close": 3.22,
         "volume": 300, "is_complete": True}])
    second = _ok(server, path)
    assert view.computed_points == 21
    assert second["times"][:-1] == first["times"][1:]
    for name, values in _values(second).items():
        assert values[:-1] == _values(first)[name][1:]


def test_one_calculation_at_a_time(server, monkeypatch):
    active, peak, original = [0], [0], indicators_view._last_row

    def slow(indicators, tail):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.002)
        active[0] -= 1
        return original(indicators, tail)

    monkeypatch.setattr(indicators_view, "_last_row", slow)
    results = []
    paths = ["/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd&limit=15",
             "/api/v1/candles/GAZP/15m/indicators?binding=future-ng-ma&limit=15"]
    threads = [threading.Thread(target=lambda path=path: results.append(_get(server, path)[0])) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [200, 200] and peak[0] == 1


def test_reads_do_not_change_market_database(server, store):
    def dump():
        with sqlite3.connect(store.path) as conn:
            return list(conn.iterdump())

    before = dump()
    for index in range(20):
        mode = "strategy" if index % 2 else "continuous"
        _ok(server, f"/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-ma&mode={mode}&limit=5")
    assert dump() == before
