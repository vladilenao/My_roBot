from datetime import datetime, timezone

import pytest

from src.portfolio import (
    ContractMeta,
    Position,
    PositionManager,
    Signal,
)

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
        take_profit=None,
        timeframe="1h",
        source="ma [basic_levels]",
    )
    base.update(kw)
    return Signal(**base)


def _manager(initial=100000, max_risk=2.0):
    return PositionManager(state=None, initial_deposit=initial, max_risk_pct=max_risk)


class TestSizingFormula:
    def test_floor_quantity(self):
        mgr = _manager()
        out = mgr.evaluate_signals([_signal()], {"NG": NG_META})[0]
        # бюджет = 100000*2% = 2000; на контракт: (2/1)*100 = 200 => qty=10
        assert out.qty == 10
        assert out.risk_rub == pytest.approx(2000.0)
        assert out.risk_pct == pytest.approx(2.0)

    def test_fractional_floor(self):
        mgr = _manager()
        sig = _signal(stop_distance_pct=3.0)
        out = mgr.evaluate_signals([sig], {"NG": NG_META})[0]
        assert out.qty == 6  # 2000 / 300

    def test_qty_zero_with_wide_stop(self):
        mgr = _manager(initial=100000, max_risk=0.1)  # бюджет 100
        out = mgr.evaluate_signals([_signal()], {"NG": NG_META})[0]
        assert out.qty == 0

    def test_stop_missing_rejected(self):
        mgr = _manager()
        out = mgr.evaluate_signals([_signal(stop_distance_pct=None)], {"NG": NG_META})[0]
        assert out.qty == 0
        assert out.reason == "stop-missing"

    def test_zero_stop_distance_rejected(self):
        mgr = _manager()
        out = mgr.evaluate_signals([_signal(stop_distance_pct=0.0)], {"NG": NG_META})[0]
        assert out.qty == 0
        assert out.reason == "stop-missing"

    def test_risk_above_max_is_capped(self):
        mgr = _manager()
        out = mgr.evaluate_signals([_signal(risk_pct=10.0)], {"NG": NG_META})[0]
        assert out.risk_pct == pytest.approx(2.0)
        assert out.risk_rub == pytest.approx(2000.0)
        assert out.qty == 10

    def test_no_contract_meta_rejected(self):
        mgr = _manager()
        out = mgr.evaluate_signals([_signal()], {})[0]
        assert out.qty == 0
        assert out.reason == "no-contract-meta"

    def test_fifo_order_preserved(self):
        mgr = _manager()
        outs = mgr.evaluate_signals(
            [_signal(position_id="BR-1", ticker="BR"), _signal(position_id="NG-1", ticker="NG")],
            {"NG": NG_META, "BR": NG_META},
        )
        assert [o.signal.ticker for o in outs] == ["BR", "NG"]

    def test_existing_position_blocks_same_ticker(self):
        mgr = _manager()
        mgr.positions["NG-123"] = Position(
            position_id="NG-123", ticker="NG", side="BUY", qty=2,
            avg_price=100.0, stop_price=98.0, take_profit=None, ts_entry=datetime.now(UTC),
        )
        out = mgr.evaluate_signals([_signal()], {"NG": NG_META})[0]
        assert out.qty == 0
        assert out.reason == "otherexisting"


class TestOverRisk:
    def test_marks_and_cancels_oldest_non_over_risk(self):
        mgr = _manager(initial=1000, max_risk=2.0)  # кап перекоса = 3000
        # NG-позиция уже превышает порог (1 лот по 100 = 10000)
        mgr.positions["NG-1"] = Position(
            position_id="NG-1", ticker="NG", side="BUY", qty=1,
            avg_price=100.0, stop_price=98.0, take_profit=None, ts_entry=datetime.now(UTC),
        )
        # две отложенные заявки других позиций
        mgr.pending = _pending_orders(["BR-1", "CL-1"])
        cancels = mgr.track_bar({"NG": 100.0, "BR": 100.0, "CL": 100.0}, {"NG": NG_META, "BR": NG_META, "CL": NG_META})
        assert mgr.positions["NG-1"].over_risk is True
        assert cancels == ["BR-1"]  # самая старая не-over-risk заявка

    def test_no_cancel_without_pending(self):
        mgr = _manager(initial=1000, max_risk=2.0)
        mgr.positions["NG-1"] = Position(
            position_id="NG-1", ticker="NG", side="BUY", qty=1,
            avg_price=10.0, stop_price=9.0, take_profit=None, ts_entry=datetime.now(UTC),
        )
        cancels = mgr.track_bar({"NG": 100.0}, {"NG": NG_META})
        assert cancels == []


class TestMarginAndGo:
    def test_margin_ok(self):
        mgr = _manager()
        assert mgr.margin_ok(1, NG_META, "BUY") is True
        assert mgr.margin_ok(30, NG_META, "BUY") is False  # 150k > 100k
        assert mgr.get_go(2, NG_META, "BUY") == pytest.approx(10000.0)

    def test_position_ops(self):
        pos = Position(position_id="P", ticker="NG", side="BUY", qty=1, avg_price=100.0,
                       stop_price=98.0, take_profit=None, ts_entry=datetime.now(UTC))
        pos.average(102.0, 1)
        assert pos.qty == 2
        assert pos.avg_price == pytest.approx(101.0)
        pos.reduce(1)
        assert pos.qty == 1
        assert pos.avg_price == pytest.approx(101.0)
        pos.mark_over_risk()
        assert pos.over_risk is True


def _pending_orders(pids):
    from src.trade_journal import RestoredOrder

    now = datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC)
    return {
        i: RestoredOrder(
            order_id=i + 1,
            position_id=pid,
            ticker=pid.split("-", 1)[0],
            side="BUY",
            qty=1,
            limit_price=100.0,
            stop_price=98.0,
            take_profit=None,
            timeframe="1h",
            ts_order=now,
            risk_pct=2.0,
            risk_rub=2000.0,
            ttl=14400,
        )
        for i, pid in enumerate(pids)
    }