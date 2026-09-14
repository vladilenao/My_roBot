from datetime import datetime, timedelta, timezone

import pytest

from src.broker import JournalBroker
from src.portfolio import ContractMeta, OrderResult, OrderStatus, PositionManager, Signal
from src.trade_journal import TradeJournal

UTC = timezone.utc

NG_META = ContractMeta(ticker="NG", price_step=1.0, step_cost=100.0, go_buy=5000.0, go_sell=5000.0)


def _signal(**kw):
    base = dict(
        position_id="NG-123",
        ticker="NG",
        side="BUY",
        entry_price=100.0,
        stop_price=98.0,
        stop_distance_pct=2.0,
        risk_pct=2.0,
        risk_rub=2000.0,
        take_profit=None,
        timeframe="1h",
        source="ma [basic_levels]",
        qty=10,
    )
    base.update(kw)
    return Signal(**base)


def _broker(tmp_path, initial=100000, max_risk=2.0):
    journal = TradeJournal.created_on_init(tmp_path / "j.csv")
    mgr = PositionManager(None, initial_deposit=initial, max_risk_pct=max_risk)
    return JournalBroker(journal, mgr, ["14:05", "19:00"])


def _bars(prices):
    return {t: (low, high, close) for t, (low, high, close) in prices.items()}


NOW = datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC)


class TestPlaceOrder:
    def test_entry_places_pending_order(self, tmp_path):
        broker = _broker(tmp_path)
        res = broker.place_order(_signal(), NG_META, NOW)
        assert res.status is OrderStatus.NEW
        assert res.qty == 10
        assert res.position_id == "NG-123"
        assert len(broker.manager.pending) == 1
        events = broker.journal.events()
        assert events[0].status == "NEW"
        assert events[0].entry_price == "100.0"
        assert events[0].reason == "ma [basic_levels]"
        assert "tf=1h" in events[0].notes

    def test_rejects_without_quantity(self, tmp_path):
        broker = _broker(tmp_path)
        res = broker.place_order(_signal(qty=0), NG_META, NOW)
        assert res.status is OrderStatus.CANCELLED
        assert res.reason == "qty-negative"

    def test_rejects_when_position_open(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(), NG_META, NOW)
        broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (99.0, 101.0, 100.0)}), {"NG": NG_META})
        res = broker.place_order(_signal(position_id="NG-999"), NG_META, NOW + timedelta(minutes=2))
        assert res.status is OrderStatus.CANCELLED
        assert res.reason == "otherexisting"

    def test_rejects_without_margin(self, tmp_path):
        broker = _broker(tmp_path, initial=4000)
        res = broker.place_order(_signal(qty=1), NG_META, NOW)
        assert res.status is OrderStatus.CANCELLED
        assert res.reason == "margin"


class TestEntryFill:
    def test_buy_fills_when_low_le_limit(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(), NG_META, NOW)
        results = broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (99.0, 100.5, 99.5)}), {"NG": NG_META})
        assert len(broker.manager.positions) == 1
        pos = next(iter(broker.manager.positions.values()))
        assert pos.qty == 10
        assert pos.avg_price == 100.0
        assert pos.protective is not None
        assert any(r.status is OrderStatus.FILLED for r in results)
        filled = broker.journal.events()
        statuses = [e.status for e in filled]
        assert statuses == ["NEW", "FILLED"]

    def test_buy_does_not_fill_when_not_reached(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(), NG_META, NOW)
        broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (100.5, 101.5, 101.0)}), {"NG": NG_META})
        assert broker.manager.positions == {}
        assert len(broker.manager.pending) == 1

    def test_sell_fills_when_high_ge_limit(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(side="SELL", stop_price=102.0), NG_META, NOW)
        results = broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (49.5, 101.5, 50.0)}), {"NG": NG_META})
        assert len(broker.manager.positions) == 1
        assert any(r.status is OrderStatus.FILLED for r in results)


class TestProtective:
    def test_stop_triggers_close(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(), NG_META, NOW)
        broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (99.0, 101.0, 100.0)}), {"NG": NG_META})
        results = broker.track_bar(NOW + timedelta(minutes=2), _bars({"NG": (97.0, 99.0, 98.5)}), {"NG": NG_META})
        assert broker.manager.positions == {}
        closes = [r for r in results if r.status is OrderStatus.FILLED]
        assert closes and closes[0].reason == "protective"
        rows = broker.journal.events()
        close_row = [e for e in rows if e.status == "FILLED" and e.side == "SELL"]
        assert close_row and close_row[0].reason == "protective"
        assert close_row[0].exit_price == "98.0"
        # qty=10, pnl = (98-100)/1*100*10 = -2000, fee = 2.0, total = -2002
        assert broker.manager.account.realized_total == pytest.approx(-2002.0)

    def test_take_profit_triggers_close(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(take_profit=105.0), NG_META, NOW)
        broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (99.0, 101.0, 100.0)}), {"NG": NG_META})
        results = broker.track_bar(NOW + timedelta(minutes=2), _bars({"NG": (104.0, 106.0, 105.5)}), {"NG": NG_META})
        closes = [r for r in results if r.status is OrderStatus.FILLED]
        assert closes and closes[0].reason == "tp"
        assert broker.manager.positions == {}

    def test_stop_priority_over_take(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(stop_price=98.0, take_profit=105.0), NG_META, NOW)
        # сначала исполним вход: low=99 <= limit=100
        broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (99.0, 101.0, 100.0)}), {"NG": NG_META})
        # теперь bar: low=97 (стоп), high=106 (тейк) → стоп должен отработать
        broker.track_bar(NOW + timedelta(minutes=2), _bars({"NG": (97.0, 106.0, 98.0)}), {"NG": NG_META})
        closes = [e for e in broker.journal.events() if e.status == "FILLED" and e.side == "SELL"]
        assert closes[0].reason == "protective"


class TestTtl:
    def test_short_timeframe_expires(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(timeframe="30m"), NG_META, NOW)
        results = broker.track_bar(NOW + timedelta(seconds=3601), _bars({"NG": (100.5, 101.5, 101.0)}), {"NG": NG_META})
        assert any(r.status is OrderStatus.EXPIRED for r in results)
        assert len(broker.manager.pending) == 0
        rows = broker.journal.events()
        assert rows[-1].status == "EXPIRED"
        assert rows[-1].reason == "ttl"

    def test_long_timeframe_lives_to_next_clearing(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(timeframe="1d"), NG_META, NOW)
        # 10:00 UTC → следующий клиринг 14:05 МСК = 11:05 UTC; через 30 минут (10:30 UTC) жив
        statuses = broker.track_bar(NOW + timedelta(minutes=30), _bars({"NG": (100.5, 101.5, 101.0)}), {"NG": NG_META})
        assert not any(r.status is OrderStatus.EXPIRED for r in statuses)
        assert len(broker.manager.pending) == 1
        # через 2 часа (12:00 UTC > 11:05 UTC) — истекает
        broker.track_bar(NOW + timedelta(hours=2), _bars({"NG": (100.5, 101.5, 101.0)}), {"NG": NG_META})
        assert len(broker.manager.pending) == 0


class TestClearing:
    def test_clearing_cancels_pending_and_snapshots(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(), NG_META, NOW)
        res = broker.run_clearing(NOW + timedelta(hours=7))
        cancelled = [r for r in res if r.status is OrderStatus.CANCELLED]
        assert cancelled and cancelled[0].reason == "clearing"
        rows = broker.journal.events()
        snapshot = [e for e in rows if e.status == "CLEARING"]
        assert snapshot and snapshot[0].deposit == "100000.0"
        assert snapshot[0].reason == "clearing"
        assert broker.manager.pending == {}

    def test_clearing_keeps_open_positions_and_replaces_protective(self, tmp_path):
        broker = _broker(tmp_path)
        broker.place_order(_signal(), NG_META, NOW)
        broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (99.0, 101.0, 100.0)}), {"NG": NG_META})
        broker.run_clearing(NOW + timedelta(hours=7))
        pos = next(iter(broker.manager.positions.values()))
        assert pos.qty == 10
        assert pos.protective is not None
        snapshot = [e for e in broker.journal.events() if e.status == "CLEARING"]
        assert snapshot and snapshot[0].qty == "1"

    def test_clearing_if_due(self, tmp_path):
        broker = _broker(tmp_path)
        assert broker.run_clearing_if_due(datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC)) is False
        assert broker.run_clearing_if_due(datetime(2026, 9, 14, 14, 20, 0, tzinfo=UTC)) is True
        assert broker.run_clearing_if_due(datetime(2026, 9, 14, 15, 0, 0, tzinfo=UTC)) is False


class TestOverRiskAndFifo:
    LOW_GO_META = ContractMeta(ticker="NG", price_step=1.0, step_cost=100.0, go_buy=100.0, go_sell=100.0)

    def test_over_risk_counter_close_on_fill(self, tmp_path):
        broker = _broker(tmp_path, initial=1000)  # кап перекоса = 3000
        broker.place_order(_signal(qty=1), self.LOW_GO_META, NOW)
        results = broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (99.0, 101.0, 100.0)}), {"NG": self.LOW_GO_META})
        assert broker.manager.positions == {}  # контр-сделка закрыла позицию
        rows = broker.journal.events()
        entry = [e for e in rows if e.status == "FILLED" and e.side == "BUY"][0]
        assert "over_risk=true" in entry.notes
        close = [e for e in rows if e.status == "FILLED" and e.side == "SELL"][0]
        assert close.reason == "over_risk"
        types = {e.type for e in broker.drain_events()}
        assert "over_risk" in types
        assert any(r.status is OrderStatus.FILLED for r in results)

    def test_fifo_cancel_of_non_over_risk_order(self, tmp_path):
        broker = _broker(tmp_path, initial=1000)  # кап перекоса = 3000
        # открытая NG-позиция: 1 лот по цене 20 → стоимость 2000 < 3000
        broker.place_order(_signal(entry_price=20.0, stop_price=19.0, qty=1, risk_rub=20.0), self.LOW_GO_META, NOW)
        broker.track_bar(NOW + timedelta(minutes=1), _bars({"NG": (19.5, 21.0, 20.0)}), {"NG": self.LOW_GO_META})
        assert len(broker.manager.positions) == 1
        # отложенная BR-заявка (не over-risk)
        broker.place_order(_signal(position_id="BR-1", ticker="BR", entry_price=50.0, stop_price=49.0,
                                   qty=1, stop_distance_pct=2.0), self.LOW_GO_META, NOW + timedelta(minutes=2))
        # рост цены NG → стоимость 10000 > 3000 → перекос: отмена BR + контр-сделка по NG
        results = broker.track_bar(
            NOW + timedelta(minutes=3),
            _bars({"NG": (99.0, 101.0, 100.0), "BR": (49.5, 52.0, 51.0)}),
            {"NG": self.LOW_GO_META, "BR": self.LOW_GO_META},
        )
        cancelled = [r for r in results if r.status is OrderStatus.CANCELLED]
        assert cancelled and cancelled[0].reason == "risk_cap"
        assert broker.manager.positions == {}  # NG закрыт контр-сделкой
        rows = broker.journal.events()
        assert any(e.status == "CANCELLED" and e.reason == "risk_cap" for e in rows)
        rows = broker.journal.events()
        assert any(e.status == "CANCELLED" and e.reason == "risk_cap" for e in rows)


class TestCancelOrder:
    def test_cancel_writes_journal_and_removes_pending(self, tmp_path):
        broker = _broker(tmp_path)
        res = broker.place_order(_signal(), NG_META, NOW)
        cancelled = broker.cancel_order(res.order_id, "risk_cap")
        assert cancelled.status is OrderStatus.CANCELLED
        assert broker.manager.pending == {}
        rows = broker.journal.events()
        assert any(e.status == "CANCELLED" and e.reason == "risk_cap" for e in rows)