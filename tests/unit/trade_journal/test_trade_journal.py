from datetime import datetime, timezone

import pytest

from src.trade_journal import (
    COLUMNS,
    COLUMNS_RU,
    JournalEvent,
    TradeJournal,
    make_position_id,
    parse_hhmm,
)

UTC = timezone.utc


def _journal(tmp_path, name="journal.csv"):
    return TradeJournal.created_on_init(tmp_path / name)


def _ev(journal, **kw):
    base = dict(
        id=kw.get("id", journal.next_id),
        position_id=kw.get("position_id", "NG-1"),
        status=kw.get("status", "NEW"),
        ts_order=kw.get("ts_order", "2026-09-14 10:00:00"),
        date="",
        side=kw.get("side", "BUY"),
        ts_entry=kw.get("ts_entry", ""),
        deposit=kw.get("deposit", ""),
        risk_pct=kw.get("risk_pct", "2.0"),
        risk_rub=kw.get("risk_rub", "2000.0"),
        entry_price=kw.get("entry_price", "100.0"),
        stop_price=kw.get("stop_price", "98.0"),
        qty=kw.get("qty", "2"),
        exit_price=kw.get("exit_price", ""),
        pnl_rub=kw.get("pnl_rub", ""),
        fee_rub=kw.get("fee_rub", ""),
        price_step=kw.get("price_step", "1.0"),
        step_cost=kw.get("step_cost", "100.0"),
        go_buy=kw.get("go_buy", "5000.0"),
        go_sell=kw.get("go_sell", "5000.0"),
        contract=kw.get("contract", "NG"),
        reason=kw.get("reason", ""),
        notes=kw.get("notes", "tf=1h"),
    )
    return JournalEvent(**base)


class TestJournalBasics:
    def test_header_written_on_init_in_russian(self, tmp_path):
        path = tmp_path / "j.csv"
        TradeJournal.created_on_init(path)
        raw = path.read_text(encoding="utf-8-sig")
        header = raw.splitlines()[0]
        cols = header.split(",")
        assert cols == COLUMNS_RU

    def test_columns_align_with_header_labels(self):
        assert len(COLUMNS) == len(COLUMNS_RU) == 23

    def test_append_only_grows_file(self, tmp_path):
        j = _journal(tmp_path)
        j.append(_ev(j, id=j.next_id))
        j.append(_ev(j, id=j.next_id, position_id="NG-2"))
        lines = j.path.read_text(encoding="utf-8-sig").splitlines()
        assert len(lines) == 3  # заголовок + 2 события

    def test_ids_monotonic(self, tmp_path):
        j = _journal(tmp_path)
        j.append(_ev(j, id=j.next_id))
        assert j.next_id == 2

    def test_position_id_format(self):
        pid = make_position_id("NG")
        assert pid.startswith("NG-")
        assert pid.split("-")[0] == "NG"

    def test_parse_hhmm(self):
        assert parse_hhmm("14:05") == __import__("datetime").timedelta(hours=14, minutes=5)


class TestJournalReplay:
    def test_empty_journal_keeps_initial_deposit(self, tmp_path):
        j = _journal(tmp_path)
        state = j.replay(initial_deposit=100000)
        assert state.balance == 100000
        assert state.positions == {}
        assert state.orders == {}

    def test_restores_position_and_pending_order(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, position_id=pid, status="NEW", qty="2", notes="tf=1h source=ma [basic_levels]"))
        j.append(_ev(j, id=j.next_id, position_id=pid, status="FILLED", ts_entry="2026-09-14 10:00:00", qty="2"))
        # вторая позиция с отложенной заявкой, не исполненной
        pid2 = make_position_id("BR")
        j.append(_ev(j, id=j.next_id, position_id=pid2, status="NEW", qty="1", side="SELL", entry_price="50.0", stop_price="52.0"))

        state = j.replay(initial_deposit=100000)

        assert pid in state.positions
        pos = state.positions[pid]
        assert pos.qty == 2
        assert pos.avg_price == 100.0
        assert pos.side == "BUY"
        assert pid2 not in state.positions
        assert len(state.orders) == 1
        order = state.orders[next(iter(state.orders))]
        assert order.position_id == pid2
        assert order.side == "SELL"
        assert order.timeframe == "1h"

    def test_realized_pnl_and_closure(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, position_id=pid, status="NEW", qty="2"))
        j.append(_ev(j, id=j.next_id, position_id=pid, status="FILLED", qty="2", ts_entry="2026-09-14 10:00:00"))
        j.append(_ev(j, id=j.next_id, position_id=pid, status="FILLED", side="SELL", qty="2",
                     exit_price="102.0", pnl_rub="400.0", fee_rub="0.4"))

        state = j.replay(initial_deposit=100000)

        assert pid not in state.positions  # закрыта
        assert state.realized == pytest.approx(400.0)

    def test_clearing_snapshot_overrides_balance(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, position_id=pid, status="NEW", qty="2"))
        j.append(_ev(j, id=j.next_id, position_id=pid, status="FILLED", qty="2", ts_entry="2026-09-14 10:00:00"))
        j.append(_ev(j, id=j.next_id, position_id=pid, status="FILLED", side="SELL", qty="2",
                     exit_price="102.0", pnl_rub="400.0", fee_rub="0.4"))
        j.append(_ev(j, id=j.next_id, position_id="", status="CLEARING", side="",
                     deposit="100400.0", pnl_rub="400.0", qty="0", reason="clearing"))

        state = j.replay(initial_deposit=100000)

        assert state.balance == pytest.approx(100400.0)

    def test_over_risk_token_marks_position(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, position_id=pid, status="NEW", qty="2"))
        j.append(_ev(j, id=j.next_id, position_id=pid, status="FILLED", ts_entry="2026-09-14 10:00:00",
                     qty="2", notes="tf=1h over_risk=true"))

        state = j.replay(initial_deposit=100000)

        assert state.positions[pid].over_risk is True


class TestJournalMalformedRows:
    def test_broken_rows_are_skipped(self, tmp_path):
        path = tmp_path / "j.csv"
        path.write_text(
            "id,position_id,Статус,Время заявки,Дата,Сторона,Время входа,Депозит,Риск %,Риск руб,"
            "Цена входа,Цена стопа,Кол-во,Цена выхода,Прибыль руб,Комиссия руб,Шаг цены,Стоимость шага,"
            "ГО (покупка),ГО (продажа),Контракт,Причина,Заметки\n"
            "1,NG-1,NEW,2026-09-14 10:00:00,,BUY,,,,100.0,98.0,2,,,,,,,,,NG,,tf=1h\n"
            "broken,line,,,,,,,,,,,,,,,,,,,,,\n"
            "2,NG-1,FILLED,2026-09-14 10:00:00,,BUY,2026-09-14 10:00:00,,,,100.0,98.0,2,,,,,,,,,NG,,tf=1h\n",
            encoding="utf-8-sig",
        )
        j = TradeJournal(path, next_event_id=1)
        events = j.events()
        assert len(events) == 2
        assert events[0].status == "NEW"
        assert events[1].status == "FILLED"

    def test_replay_from_russian_header_roundtrip(self, tmp_path):
        # после записи с русскими заголовками replay читает то же состояние
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, position_id=pid, status="NEW", qty="2", notes="tf=1h"))
        j.append(_ev(j, id=j.next_id, position_id=pid, status="FILLED", ts_entry="2026-09-14 10:00:00", qty="2"))

        state = j.replay(initial_deposit=100000)

        assert state.positions[pid].qty == 2
        assert state.positions[pid].avg_price == 100.0

    def test_unknown_status_rejected_on_append(self, tmp_path):
        j = _journal(tmp_path)
        with pytest.raises(ValueError, match="статус"):
            j.append(_ev(j, id=j.next_id, status="PENDING"))

    def test_non_monotonic_id_rejected(self, tmp_path):
        j = _journal(tmp_path)
        j.append(_ev(j, id=j.next_id))
        with pytest.raises(ValueError):
            j.append(_ev(j, id=1))