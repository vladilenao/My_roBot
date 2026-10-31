"""Позиции, сделки и итоги контура по журналу сделок (add-contour-trades-api)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http import HTTPStatus
from urllib import error, request

import pytest

from src.bot import control_views as views
from src.bot.launch_control import LaunchControl
from src.bot.trades_view import CURVE_MAX_POINTS, ContourTrades, TradesError, _thin, exit_kind, trade_ref
from src.broker import ExecutionEvent, ExecutionStatus
from src.broker.port import FeeSource
from src.data.export_api import MarketDataExportServer
from src.trade_journal.reducer import ExecutionReducer
from src.trade_journal.storage import Storage
from src.trade_management.actions import CloseTrade, OpenTrade, ReduceTrade
from src.trade_management.manager import TradeManager
from src.trade_management.models import ProfileSnapshot, TargetPlan, TradePlan
from tests.unit.bot.test_launch_control import CONTROL_KEY, EXPORT_KEY, _call, _problem
from tests.unit.clusters.test_report import _check

UTC = timezone.utc
# Сейчас 31.10.2026 20:10 МСК: торговый день начался в 19:00 МСК (16:00 UTC).
NOW = datetime(2026, 10, 31, 17, 10, tzinfo=UTC)
NAMES = {"NGX6": "NG-11.26", "SIZ6": "Si-12.26", "BRZ6": "BR-12.26", "BRX6": "BR-11.26"}
TICKERS = tuple(NAMES)
CATALOG = {
    "future-ng-macd": {"id": "future-ng-macd", "strategy": "macd_rsi_stoch", "timeframe": "15m"},
    "future-si-levels": {"id": "future-si-levels", "strategy": "levels", "timeframe": "1h"},
    "future-br-macd": {"id": "future-br-macd", "strategy": "macd_rsi_stoch", "timeframe": "4h"},
}


def _at(hour, minute=0, day=31):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


class _Broker:
    def register_trade(self, plan) -> None:
        return None


class _Journal:
    """Журнал контура, собранный штатными менеджером и редьюсером."""

    def __init__(self, path):
        self.storage = Storage(path, initial_deposit="100000")
        self.reducer = ExecutionReducer(self.storage)
        self.manager = TradeManager(self.storage, _Broker(), initial_balance=Decimal("100000"))
        self.storage.set_names(NAMES)
        self._commands = 0

    def open(self, trade_id, assignment, ticker, side, lots, price, when, *, step="1", cost="1", targets=(),
             fills=None):
        """Вход; ``fills`` — частичные исполнения заявки входа ``(лоты, цена, время)``."""
        stop = Decimal(price) * (Decimal("0.9") if side == "BUY" else Decimal("1.1"))
        plan = TradePlan(trade_id, assignment, ticker, side, f"signal-{trade_id}", Decimal(price), stop, tuple(targets),
                         ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}), when, "1m")
        command = self._command()
        self.manager.submit_plan(plan, OpenTrade(command, trade_id, 0, "entry", lots),
                                 price_step=Decimal(step), step_cost=Decimal(cost))
        for index, (part, part_price, part_when) in enumerate(fills or [(lots, price, when)]):
            self._fill(command, trade_id, part, part_price, part_when, "entry", execution=f"{command}-{index}")

    def reduce(self, trade_id, lots, price, when, reason):
        command = self._command()
        self.manager.submit_action(ReduceTrade(command, trade_id, self._revision(trade_id), reason, lots))
        self._fill(command, trade_id, lots, price, when, reason)

    def target(self, trade_id, target_id, lots, price, when):
        """Цель исполнил брокер: защитная директива, как у живого контура."""
        self._fill(f"{trade_id}:pv:tp:{target_id}:{when:%H%M}", trade_id, lots, price, when, f"tp:{target_id}")

    def close(self, trade_id, lots, price, when, reason):
        command = self._command()
        self.manager.submit_action(CloseTrade(command, trade_id, self._revision(trade_id), reason))
        self._fill(command, trade_id, lots, price, when, reason)

    def stop(self, trade_id, lots, price, when):
        self._fill(f"{trade_id}:pv:stop:{when:%H%M}", trade_id, lots, price, when, "protective")

    def _fill(self, command, trade_id, lots, price, when, reason, execution=None):
        self.reducer.apply(ExecutionEvent(
            execution_id=f"exec-{execution or command}", order_id=None, command_id=command, trade_id=trade_id,
            status=ExecutionStatus.FILL, filled_quantity=lots, price=Decimal(price), fee=Decimal("1.00"),
            timestamp=when, reason=reason, fee_source=FeeSource.CONFIGURED))

    def _command(self):
        self._commands += 1
        return f"cmd-{self._commands}"

    def _revision(self, trade_id):
        return self.storage.connection.execute(
            "SELECT state_revision FROM trades WHERE trade_id=?", (trade_id,)).fetchone()[0]


NG = "future-ng-macd:NGX6:15m:2026-10-31T11:00:00:BUY"
SI = "future-si-levels:SIZ6:1h:2026-10-31T06:00:00:BUY"
SI_SHORT = "future-si-levels:SIZ6:1h:2026-10-31T15:00:00:SELL"
BR = "future-br-macd:BRZ6:4h:2026-10-31T16:00:00:BUY"
OLD = "future-br-old:BRX6:5m:2026-10-30T10:00:00:SELL"
PENDING = "future-ng-macd:NGX6:15m:2026-10-31T16:45:00:BUY"


def _build(path):
    journal = _Journal(path)
    # Вчера: привязки уже нет в конфигурации, закрытие по встречному сигналу.
    journal.open(OLD, "future-br-old", "BRX6", "SELL", 1, "64.60", _at(10, day=30), step="0.01", cost="8")
    journal.close(OLD, 1, "64.48", _at(11, day=30), "raw-opposite-signal")
    # Утро: частичный выход по правилу, остаток — стопом.
    journal.open(SI, "future-si-levels", "SIZ6", "BUY", 2, "82400", _at(6, 15))
    journal.reduce(SI, 1, "82422", _at(6, 55), "ma10-or-cloud-partial-exit")
    journal.stop(SI, 1, "82342", _at(7, 40))
    # День: целиком по цели.
    journal.open(NG, "future-ng-macd", "NGX6", "BUY", 2, "2.861", _at(11, 5), step="0.001", cost="1",
                 targets=(TargetPlan("tp-1", Decimal("2.884"), Decimal("1")),))
    journal.target(NG, "tp-1", 2, "2.884", _at(12, 20))
    # Торговый день (с 16:00 UTC): шорт, вход двумя исполнениями, закрытие перед концом сессии.
    journal.open(SI_SHORT, "future-si-levels", "SIZ6", "SELL", 2, "82640", _at(15, 50),
                 fills=[(1, "82640", _at(15, 50)), (1, "82700", _at(16, 10))])
    journal.close(SI_SHORT, 2, "82600", _at(16, 40), "intraday-session-end")
    # Открытая позиция и вход без исполнения.
    journal.open(BR, "future-br-macd", "BRZ6", "BUY", 3, "64.38", _at(16, 30), step="0.01", cost="8")
    journal.manager.submit_plan(
        TradePlan(PENDING, "future-ng-macd", "NGX6", "BUY", "signal-pending", Decimal("2.9"), Decimal("2.8"), (),
                  ProfileSnapshot("levels_rr", "1", {"buffer": Decimal("1")}), _at(16, 45), "1m"),
        OpenTrade("cmd-pending", PENDING, 0, "entry", 1), price_step=Decimal("0.001"), step_cost=Decimal("1"))
    journal.storage.close()


class _Store:
    """Рыночная база ядра: только то, что читают ресурсы торговли."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {"ticker": "BRZ6", "interval": "1m", "last_close": "64.12", "last_open": "2026-10-31T17:09:00Z"},
            {"ticker": "BRZ6", "interval": "15m", "last_close": "64.00", "last_open": "2026-10-31T16:45:00Z"},
        ]

    def candle_series(self, *, source):
        return self.rows


@pytest.fixture
def journal_path(tmp_path):
    path = tmp_path / "emulation" / "trades.sqlite3"
    path.parent.mkdir()
    _build(path)
    return path


def _trading(path, deposit="100000"):
    return ContourTrades(base=views.EMULATION, journal=lambda: path, catalog=CATALOG, initial_deposit=deposit,
                         clock=lambda: NOW)


@pytest.fixture
def trading(journal_path):
    return _trading(journal_path)


def _no_tickers(payload):
    text = json.dumps(payload, ensure_ascii=False)
    assert not any(ticker in text for ticker in TICKERS), text


class TestPositions:
    def test_open_position_with_price(self, trading):
        payload = trading.positions({}, _Store())
        _check(payload, {"$ref": "#/components/schemas/PositionList"})
        _no_tickers(payload)
        [item] = payload["items"]
        assert item["id"] == trade_ref(BR)
        assert item["binding"] == {"id": "future-br-macd", "strategy": "macd_rsi_stoch", "timeframe": "4h"}
        assert (item["instrument"], item["side"], item["lots"], item["entry_price"]) == ("BR-12.26", "long", 3, "64.38")
        assert (item["last_price"], item["last_price_at"]) == ("64.12", "2026-10-31T17:09:00Z")
        # (64.12 − 64.38) × 3 × 8 / 0.01
        assert item["unrealized"] == "-624.00"
        assert (item["fees"], item["realized"], item["fees_source"]) == ("1.00", "-1.00", "configured")
        assert item["entries"] == [{"at": "2026-10-31T16:30:00Z", "lots": 3, "price": "64.38"}]
        assert (item["exits"], item["phase"], item["opened_at"]) == ([], "open", "2026-10-31T16:30:00Z")
        assert (payload["unrealized"], payload["unrealized_complete"]) == ("-624.00", True)

    def test_no_market_base_no_price(self, trading):
        payload = trading.positions({}, None)
        [item] = payload["items"]
        assert (item["last_price"], item["last_price_at"], item["unrealized"]) == (None, None, None)
        assert (payload["unrealized"], payload["unrealized_complete"]) == ("0.00", False)

    def test_partial_exit_keeps_position(self, tmp_path):
        path = tmp_path / "trades.sqlite3"
        journal = _Journal(path)
        journal.open(NG, "future-ng-macd", "NGX6", "BUY", 3, "2.874", _at(12),
                     step="0.001", cost="1", targets=(TargetPlan("tp-1", Decimal("2.900"), Decimal("0.34")),))
        journal.target(NG, "tp-1", 1, "2.900", _at(13))
        journal.storage.close()
        [item] = _trading(path).positions({}, None)["items"]
        assert (item["lots"], item["phase"]) == (2, "reducing")
        assert [exit_["kind"] for exit_ in item["exits"]] == ["target"]
        # Результат цели (2.900 − 2.874) × 1 × 1 / 0.001 = 26 минус комиссии входа и выхода.
        assert (item["fees"], item["realized"]) == ("2.00", "24.00")

    def test_instrument_filter(self, trading):
        assert trading.positions({"instrument": ["br-12.26"]}, None)["items"]
        assert trading.positions({"instrument": ["NG-11.26"]}, None)["items"] == []


class TestTrades:
    def test_newest_first_with_exit_kinds(self, trading):
        payload, etag = trading.trades({})
        _check(payload, {"$ref": "#/components/schemas/TradePage"})
        _no_tickers(payload)
        assert [item["id"] for item in payload["items"]] == [trade_ref(t) for t in (SI_SHORT, NG, SI, OLD)]
        assert [item["exit"]["kind"] for item in payload["items"]] == ["session_end", "target", "stop", "signal"]
        assert etag.strip('"').isdigit() and int(etag.strip('"')) > 0
        assert "earlier" not in payload["_links"]

    def test_partial_then_stop(self, trading):
        item = next(item for item in trading.trades({})[0]["items"] if item["id"] == trade_ref(SI))
        assert (item["lots"], item["entry_price"], item["exit_price"]) == (2, "82400", "82382")
        assert [(e["kind"], e["code"], e["lots"]) for e in item["exits"]] == [
            ("partial", "ma10-or-cloud-partial-exit", 1), ("stop", "protective", 1)]
        assert item["exit"] == {"kind": "stop", "code": "protective", "label": "стоп",
                                "scenario": "частичный выход по MA10/облаку (часть) + стоп (остаток)"}
        # 22 − 58 = −36 брутто, три исполнения по рублю комиссии.
        assert (item["gross"], item["fees"], item["net"]) == ("-36.00", "3.00", "-39.00")

    def test_every_entry_fill_is_visible(self, trading):
        item = next(item for item in trading.trades({})[0]["items"] if item["id"] == trade_ref(SI_SHORT))
        assert [(e["lots"], e["price"]) for e in item["entries"]] == [(1, "82640"), (1, "82700")]
        assert (item["side"], item["lots"], item["entry_price"]) == ("short", 2, "82670")

    def test_target_label_matches_csv(self, trading):
        item = next(item for item in trading.trades({})[0]["items"] if item["id"] == trade_ref(NG))
        assert (item["exit"]["kind"], item["exit"]["code"], item["exit"]["label"]) == ("target", "tp:tp-1", "TP1")
        assert item["binding"]["strategy"] == "macd_rsi_stoch"

    def test_binding_removed_from_config(self, trading):
        item = next(item for item in trading.trades({})[0]["items"] if item["id"] == trade_ref(OLD))
        assert item["binding"] == {"id": "future-br-old", "strategy": None, "timeframe": "5m"}
        assert item["instrument"] == "BR-11.26"

    def test_pages_without_gaps(self, trading):
        first, _ = trading.trades({"limit": ["2"]})
        assert [item["id"] for item in first["items"]] == [trade_ref(SI_SHORT), trade_ref(NG)]
        earlier = first["_links"]["earlier"]["href"]
        assert earlier == "/api/v1/emulation/trades?to=2026-10-31T12:20:00Z&limit=2"
        second, _ = trading.trades({"to": ["2026-10-31T12:20:00Z"], "limit": ["2"]})
        assert [item["id"] for item in second["items"]] == [trade_ref(SI), trade_ref(OLD)]
        assert "earlier" not in second["_links"]

    def test_same_closed_at_stays_on_one_page(self, tmp_path):
        path = tmp_path / "trades.sqlite3"
        journal = _Journal(path)
        for index in range(3):
            trade = f"future-ng-macd:NGX6:15m:2026-10-31T1{index}:00:00:BUY"
            journal.open(trade, "future-ng-macd", "NGX6", "BUY", 1, "2.9", _at(10 + index))
            journal.close(trade, 1, "2.95", _at(14), "raw-opposite-signal")
        journal.storage.close()
        page, _ = _trading(path).trades({"limit": ["1"]})
        assert len(page["items"]) == 3 and "earlier" not in page["_links"]

    def test_instrument_filter_and_window(self, trading):
        page, _ = trading.trades({"instrument": ["si-12.26"], "from": ["2026-10-31T00:00:00Z"]})
        assert [item["id"] for item in page["items"]] == [trade_ref(SI_SHORT), trade_ref(SI)]

    @pytest.mark.parametrize("query", [{"from": ["2026-10-31T10:00:00Z"], "to": ["2026-10-31T09:00:00Z"]},
                                       {"from": ["2026-10-31T10:00:00"]}, {"limit": ["0"]}, {"limit": ["1001"]},
                                       {"sort": ["asc"]}])
    def test_bad_query(self, trading, query):
        with pytest.raises(TradesError) as caught:
            trading.trades(query)
        assert (caught.value.status, caught.value.code) == (HTTPStatus.BAD_REQUEST, "INVALID_REQUEST")


class TestPerformance:
    def test_trading_day(self, trading):
        payload = trading.performance({"period": ["day"]}, _Store())
        _check(payload, {"$ref": "#/components/schemas/Performance"})
        assert payload["period"] == {"name": "day", "from": "2026-10-31T16:00:00Z", "to": "2026-10-31T17:10:00Z"}
        # Шорт 2 лота: (82640 − 82600) + (82700 − 82600) = 140 брутто, три комиссии.
        assert payload["realized"] == {"net": "137.00", "gross": "140.00", "fees": "3.00", "fees_source": "configured"}
        assert payload["trades"] == {"count": 1, "winners": 1, "losers": 0}
        assert (payload["profit_factor"], payload["average_net"]) == (None, "137.00")
        assert payload["curve"] == [{"at": "2026-10-31T16:00:00Z", "net": "0.00"},
                                    {"at": "2026-10-31T16:40:00Z", "net": "137.00"}]
        assert payload["open"] == {"count": 1, "realized": "-1.00", "unrealized": "-624.00",
                                   "unrealized_complete": True}
        assert payload["curve_now"] == {"at": "2026-10-31T17:10:00Z", "net": "-488.00"}
        assert payload["_links"]["trades"] == {"href": "/api/v1/emulation/trades?from=2026-10-31T16:00:00Z"}

    def test_all_reconciles_with_account(self, trading):
        payload = trading.performance({"period": ["all"]}, None)
        account = trading.account()
        _check(account, {"$ref": "#/components/schemas/VirtualAccount"})
        assert payload["period"]["from"] is None and payload["curve"][0]["at"] == "2026-10-30T10:00:00Z"
        last = Decimal(payload["curve"][-1]["net"]) + Decimal(payload["open"]["realized"])
        assert last == Decimal(account["balance"]) - Decimal(account["initial"])
        assert payload["trades"]["count"] == 4
        assert payload["_links"]["trades"] == {"href": "/api/v1/emulation/trades"}

    def test_drawdown_and_profit_factor(self, trading):
        payload = trading.performance({"period": ["month"]}, None)
        # По закрытиям: OLD +94, SI −39, NG +44, SI_SHORT +137 → дно −39 от пика +94 после SI.
        nets = [Decimal(point["net"]) for point in payload["curve"]]
        assert nets == [Decimal("0"), Decimal("94"), Decimal("55"), Decimal("99"), Decimal("236")]
        assert payload["max_drawdown"] == {"amount": "-39.00", "at": "2026-10-31T07:40:00Z"}
        # (94 + 44 + 137) / 39
        assert payload["profit_factor"] == "7.05"
        assert payload["period"]["from"] == "2026-09-30T21:00:00Z"

    def test_custom_period(self, trading):
        payload = trading.performance({"from": ["2026-10-31T00:00:00Z"], "to": ["2026-10-31T12:00:00Z"]}, None)
        assert payload["period"]["name"] == "custom" and payload["trades"]["count"] == 1
        assert payload["_links"]["trades"] == {
            "href": "/api/v1/emulation/trades?from=2026-10-31T00:00:00Z&to=2026-10-31T12:00:00Z"}

    @pytest.mark.parametrize("query", [{"period": ["year"]}, {"period": ["day"], "from": ["2026-10-31T00:00:00Z"]}])
    def test_bad_period(self, trading, query):
        with pytest.raises(TradesError) as caught:
            trading.performance(query, None)
        assert caught.value.code == "INVALID_REQUEST"

    @pytest.mark.parametrize(("now", "start"), [
        (datetime(2026, 10, 31, 15, 59, tzinfo=UTC), "2026-10-30T16:00:00Z"),  # 18:59 МСК — ещё вчерашний день
        (datetime(2026, 10, 31, 16, 0, tzinfo=UTC), "2026-10-31T16:00:00Z"),  # 19:00 МСК — новый
        (datetime(2026, 11, 1, 11, 0, tzinfo=UTC), "2026-10-31T16:00:00Z"),  # воскресенье днём — с субботы 19:00
    ])
    def test_day_boundary(self, journal_path, now, start):
        trading = ContourTrades(base=views.EMULATION, journal=lambda: journal_path, catalog=CATALOG,
                                clock=lambda: now)
        assert trading.performance({}, None)["period"]["from"] == start

    def test_week_starts_monday_msk(self, trading):
        assert trading.performance({"period": ["week"]}, None)["period"]["from"] == "2026-10-25T21:00:00Z"

    def test_thinning_keeps_extremes(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        points = [(start + timedelta(minutes=i), Decimal(i % 97) - (Decimal(5000) if i == 777 else 0))
                  for i in range(3000)]
        thin = _thin(points)
        assert len(thin) <= CURVE_MAX_POINTS
        assert thin[0] == points[0] and thin[-1] == points[-1] and points[777] in thin


class TestEmptyAndBroken:
    def test_no_journal(self, tmp_path):
        trading = _trading(tmp_path / "missing.sqlite3")
        assert trading.positions({}, None)["items"] == []
        page, etag = trading.trades({})
        assert (page["items"], etag) == ([], '"0"')
        payload = trading.performance({}, None)
        assert payload["trades"]["count"] == 0 and len(payload["curve"]) == 1
        assert payload["realized"]["fees_source"] is None
        assert trading.account()["balance"] == "100000.00"

    def test_journal_without_tables(self, tmp_path):
        path = tmp_path / "trades.sqlite3"
        sqlite3.connect(path).close()
        payload = _trading(path).performance({"period": ["day"]}, None)
        assert (payload["trades"]["count"], payload["realized"]["net"]) == (0, "0.00")

    def test_broken_journal(self, tmp_path):
        path = tmp_path / "trades.sqlite3"
        path.write_bytes(b"not a database" * 100)
        with pytest.raises(TradesError) as caught:
            _trading(path).positions({}, None)
        assert (caught.value.status, caught.value.code) == (HTTPStatus.SERVICE_UNAVAILABLE, "JOURNAL_UNAVAILABLE")
        assert str(path) not in caught.value.detail

    def test_old_trade_without_price_step(self, journal_path, trading):
        """Сделка из журнала до миграции: без шага цены нереализованный результат не посчитать."""
        with sqlite3.connect(journal_path) as connection:
            connection.execute("UPDATE trades SET price_step=NULL WHERE trade_id=?", (BR,))
        payload = trading.positions({}, _Store())
        [item] = payload["items"]
        assert (item["last_price"], item["unrealized"]) == ("64.12", None)
        assert payload["unrealized_complete"] is False

    def test_writer_is_not_blocked_by_reader(self, journal_path, trading):
        snapshot = trading._snapshot()
        journal = snapshot.__enter__()
        try:
            assert journal.closed()
            writer = sqlite3.connect(journal_path, timeout=0.5)
            with writer:
                writer.execute("UPDATE account SET updated_at=? WHERE account_id=1", ("2026-10-31T17:00:00+00:00",))
            writer.close()
        finally:
            snapshot.__exit__(None, None, None)
        assert trading.account()["updated_at"] == "2026-10-31T17:00:00Z"

    def test_reads_do_not_touch_journal(self, journal_path, trading):
        before = journal_path.read_bytes(), journal_path.stat().st_mtime_ns
        trading.positions({}, _Store())
        trading.trades({})
        trading.performance({"period": ["all"]}, _Store())
        trading.account()
        assert (journal_path.read_bytes(), journal_path.stat().st_mtime_ns) == before


@pytest.mark.parametrize(("action", "code", "kind"), [
    ("TARGET:tp-2", "tp:tp-2", "target"), ("STOP", "protective", "stop"),
    ("REDUCE", "ma10-or-cloud-partial-exit", "partial"), ("CLOSE", "ma40-opposite-close", "signal"),
    ("CLOSE", "intraday-session-end", "session_end"), ("CLOSE", "contract-rollover", "expiry"),
    ("CLOSE", "binding-remove", "contour"), ("CLOSE", "something-new", "other"),
])
def test_exit_kind(action, code, kind):
    assert exit_kind(action, code) == kind


class TestHttp:
    @pytest.fixture
    def server(self, journal_path, tmp_path):
        srv = MarketDataExportServer(_Store(), EXPORT_KEY, port=0, control=LaunchControl(reply_timeout=2),
                                     control_token=CONTROL_KEY)
        srv.trading = {views.EMULATION: _trading(journal_path),
                       views.SANDBOX: ContourTrades(base=views.SANDBOX, journal=lambda: tmp_path / "no.sqlite3",
                                                    catalog=CATALOG, clock=lambda: NOW)}
        try:
            srv.start()
        except PermissionError:
            pytest.skip("Песочница не разрешает открыть loopback-сокет")
        yield srv
        srv.close()

    def test_routes(self, server):
        for path, schema in (("/api/v1/emulation/positions", "PositionList"), ("/api/v1/emulation/trades", "TradePage"),
                             ("/api/v1/emulation/performance?period=all", "Performance"),
                             ("/api/v1/emulation/account", "VirtualAccount"),
                             ("/api/v1/sandbox/positions", "PositionList"), ("/api/v1/sandbox/trades", "TradePage"),
                             ("/api/v1/sandbox/performance", "Performance")):
            status, payload, _ = _call(server, "GET", path, CONTROL_KEY)
            assert status == 200, (path, payload)
            _check(payload, {"$ref": f"#/components/schemas/{schema}"})
            _no_tickers(payload)

    def test_sandbox_has_no_account(self, server):
        status, payload, headers = _call(server, "GET", "/api/v1/sandbox/account", CONTROL_KEY)
        assert status == 404

    def test_etag_and_not_modified(self, server):
        status, _, headers = _call(server, "GET", "/api/v1/emulation/trades", CONTROL_KEY)
        assert status == 200
        address, port = server.address
        req = request.Request(f"http://{address}:{port}/api/v1/emulation/trades",
                              headers={"Authorization": f"Bearer {CONTROL_KEY}", "If-None-Match": headers["ETag"]})
        with pytest.raises(error.HTTPError) as caught:
            request.urlopen(req, timeout=5)
        assert caught.value.code == 304

    def test_bad_request_is_problem(self, server):
        _problem(400, *_call(server, "GET", "/api/v1/emulation/performance?period=year", CONTROL_KEY)[1:],
                 "INVALID_REQUEST")

    def test_export_key_rejected(self, server):
        status, payload, headers = _call(server, "GET", "/api/v1/emulation/positions", EXPORT_KEY)
        _problem(401, payload, headers, "UNAUTHORIZED")

    def test_foreign_host_rejected(self, server):
        status, _, _ = _call(server, "GET", "/api/v1/sandbox/trades", CONTROL_KEY, host="evil.example:8101")
        assert status == 403

    def test_contour_links(self, server):
        _, payload, _ = _call(server, "GET", "/api/v1/emulation", CONTROL_KEY)
        assert {name: payload["_links"][name]["href"] for name in ("positions", "trades", "performance", "account")} == {
            "positions": "/api/v1/emulation/positions", "trades": "/api/v1/emulation/trades",
            "performance": "/api/v1/emulation/performance", "account": "/api/v1/emulation/account"}
