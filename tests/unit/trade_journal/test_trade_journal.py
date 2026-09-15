from datetime import datetime, timedelta, timezone

import pytest

from src.trade_journal import (
    COLUMNS,
    COLUMNS_RU,
    POSITIONS_COLUMNS,
    POSITIONS_COLUMNS_RU,
    JournalEvent,
    TradeJournal,
    format_dt,
    make_position_id,
    parse_dt,
    parse_hhmm,
)

UTC = timezone.utc


def _journal(tmp_path, name="journal.csv"):
    return TradeJournal.created_on_init(tmp_path / name)


def _ev(journal, **kw):
    base = dict(
        id=kw.get("id", journal.next_id),
        op=kw.get("op", "ЗАЯВКА"),
        ts=kw.get("ts", "2026-09-14 13:00:00"),
        order_id=kw.get("order_id", str(journal.next_id)),
        position_id=kw.get("position_id", "NG-1"),
        contract=kw.get("contract", "NG-10.26"),
        side=kw.get("side", "BUY"),
        qty=kw.get("qty", "2"),
        price=kw.get("price", "100.0"),
        stop=kw.get("stop", "98.0"),
        pnl_part=kw.get("pnl_part", ""),
        fee=kw.get("fee", ""),
        deposit=kw.get("deposit", ""),
        risk_pct=kw.get("risk_pct", "2.0"),
        risk_rub=kw.get("risk_rub", "2000.0"),
        go=kw.get("go", "5000.0"),
        reason=kw.get("reason", ""),
        notes=kw.get("notes", "tf=1h"),
    )
    return JournalEvent(**base)


class TestJournalBasics:
    def test_header_written_on_init_in_russian(self, tmp_path):
        path = tmp_path / "j.csv"
        TradeJournal.created_on_init(path)
        header = path.read_text(encoding="utf-8-sig").splitlines()[0]
        assert header.split(",") == COLUMNS_RU

    def test_columns_align_with_header_labels(self):
        assert len(COLUMNS) == len(COLUMNS_RU) == 18

    def test_positions_columns_align(self):
        assert len(POSITIONS_COLUMNS) == len(POSITIONS_COLUMNS_RU) == 16

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

    def test_parse_hhmm(self):
        assert parse_hhmm("14:05") == timedelta(hours=14, minutes=5)

    def test_positions_path_derived_from_journal(self, tmp_path):
        j = TradeJournal.created_on_init(tmp_path / "trade_journal.csv")
        assert j.positions_path.name == "trade_journal_positions.csv"

    def test_custom_positions_path(self, tmp_path):
        j = TradeJournal.created_on_init(
            tmp_path / "journal.csv", positions_path=tmp_path / "cards.csv"
        )
        assert j.positions_path.name == "cards.csv"

    def test_positions_file_written_after_append(self, tmp_path):
        j = _journal(tmp_path)
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id="NG-1", qty="2"))
        lines = j.positions_path.read_text(encoding="utf-8-sig").splitlines()
        assert lines[0].split(",") == POSITIONS_COLUMNS_RU
        assert len(lines) == 2


class TestTimesMsk:
    def test_format_dt_writes_msk(self):
        dt = datetime(2026, 9, 14, 19, 22, 0, tzinfo=UTC)
        assert format_dt(dt) == "2026-09-14 22:22:00"

    def test_parse_dt_reads_msk_as_utc(self):
        assert parse_dt("2026-09-14 22:22:00") == datetime(2026, 9, 14, 19, 22, 0, tzinfo=UTC)

    def test_roundtrip(self):
        now = datetime(2026, 9, 14, 19, 22, 0, tzinfo=UTC)
        assert parse_dt(format_dt(now)) == now


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
        j.append(_ev(j, id=j.next_id, op="ЗАЯВКА", position_id=pid, qty="2",
                     notes="tf=1h source=ma [basic_levels]"))
        j.append(_ev(j, id=j.next_id, op="ВХОД", order_id="1", position_id=pid, qty="2",
                     price="100.0", ts="2026-09-14 10:00:00"))
        pid2 = make_position_id("BR")
        j.append(_ev(j, id=j.next_id, op="ЗАЯВКА", position_id=pid2, qty="1", side="SELL",
                     price="50.0", stop="52.0"))

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

    def test_cancelled_pending_order_not_restored(self, tmp_path):
        j = _journal(tmp_path)
        pid2 = make_position_id("BR")
        j.append(_ev(j, id=j.next_id, op="ЗАЯВКА", position_id=pid2, qty="1"))
        j.append(_ev(j, id=j.next_id, op="ОТМЕНА", order_id="1", position_id=pid2, qty="1"))

        state = j.replay(initial_deposit=100000)

        assert state.orders == {}

    def test_realized_pnl_and_closure(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ЗАЯВКА", position_id=pid, qty="2"))
        j.append(_ev(j, id=j.next_id, op="ВХОД", order_id="1", position_id=pid, qty="2", price="100.0"))
        j.append(_ev(j, id=j.next_id, op="ВЫХОД", position_id=pid, qty="2", price="102.0",
                     pnl_part="400.0", fee="0.4"))

        state = j.replay(initial_deposit=100000)

        assert pid not in state.positions
        assert state.realized == pytest.approx(400.0)

    def test_clearing_snapshot_overrides_balance(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id=pid, qty="2", price="100.0"))
        j.append(_ev(j, id=j.next_id, op="ВЫХОД", position_id=pid, qty="2", price="102.0",
                     pnl_part="400.0", fee="0.4"))
        j.append(_ev(j, id=j.next_id, op="СНИМОК", position_id="", side="",
                     qty="0", deposit="100400.0", pnl_part="400.0", reason="clearing"))

        state = j.replay(initial_deposit=100000)

        assert state.balance == pytest.approx(100400.0)
        assert state.realized == pytest.approx(400.0)

    def test_add_averages_price(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id=pid, qty="2", price="100.0"))
        j.append(_ev(j, id=j.next_id, op="ДОБОР", position_id=pid, qty="1", price="103.0"))

        state = j.replay(initial_deposit=100000)

        pos = state.positions[pid]
        assert pos.qty == 3
        assert pos.avg_price == pytest.approx(101.0)

    def test_partial_take_reduces_but_keeps_open(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id=pid, qty="5", price="100.0"))
        j.append(_ev(j, id=j.next_id, op="ТЕЙК", position_id=pid, qty="2", price="102.0",
                     pnl_part="400.0", fee="0.4"))

        state = j.replay(initial_deposit=100000)

        assert state.positions[pid].qty == 3
        assert state.realized == pytest.approx(400.0)

    def test_over_risk_token_marks_position(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id=pid, qty="2", price="100.0",
                     notes="tf=1h over_risk=true"))

        state = j.replay(initial_deposit=100000)

        assert state.positions[pid].over_risk is True


class TestPositionCards:
    def test_full_scenario_card(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ЗАЯВКА", position_id=pid, qty="5", price="100.0",
                     stop="98.0", go="5000.0", notes="tf=1h"))
        j.append(_ev(j, id=j.next_id, op="ВХОД", order_id="1", position_id=pid, qty="5",
                     price="100.0", stop="98.0", go="5000.0"))
        j.append(_ev(j, id=j.next_id, op="ДОБОР", order_id="2", position_id=pid, qty="2",
                     price="103.0", stop="98.0", go="5000.0"))
        j.append(_ev(j, id=j.next_id, op="ТЕЙК", order_id="3", position_id=pid, qty="2",
                     price="105.0", pnl_part="900.0", fee="0.9"))
        j.append(_ev(j, id=j.next_id, op="ВЫХОД", order_id="4", position_id=pid, qty="5",
                     price="101.0", pnl_part="1667.0", fee="1.7"))

        cards = j.cards()
        assert len(cards) == 1
        card = cards[0]
        assert card.status == "ЗАКРЫТА"
        assert card.qty == 0
        assert card.avg_price == pytest.approx(706.0 / 7)
        assert card.pnl == pytest.approx(2567.0)
        assert card.fee == pytest.approx(2.6)
        assert card.go_max == 5000.0
        assert card.contract == "NG-10.26"
        assert card.ts_exit == j.events()[-1].ts
        assert card.exit_price == pytest.approx(715.0 / 7)

    def test_open_position_card(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id=pid, qty="5", price="100.0"))

        card = j.cards()[0]
        assert card.status == "ОТКРЫТА"
        assert card.qty == 5
        assert card.ts_entry
        assert card.ts_exit == ""
        assert card.exit_price is None

    def test_cancelled_order_card(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ЗАЯВКА", position_id=pid, qty="5"))
        j.append(_ev(j, id=j.next_id, op="ОТМЕНА", order_id="1", position_id=pid, qty="5", reason="ttl"))

        card = j.cards()[0]
        assert card.status == "ОТМЕНЕНА"
        assert card.qty == 0

    def test_standalone_rejection_creates_no_card(self, tmp_path):
        j = _journal(tmp_path)
        j.append(_ev(j, id=j.next_id, op="ОТМЕНА", position_id="NG-x", qty="0", reason="duplicate"))

        assert j.cards() == []

    def test_partial_take_keeps_card_open(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id=pid, qty="5", price="100.0"))
        j.append(_ev(j, id=j.next_id, op="ТЕЙК", position_id=pid, qty="2", price="102.0", pnl_part="400.0"))

        card = j.cards()[0]
        assert card.status == "ОТКРЫТА"
        assert card.qty == 3
        assert card.pnl == pytest.approx(400.0)

    def test_cards_rebuilt_from_ledger_on_reload(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id=pid, qty="2", price="100.0"))

        reloaded = TradeJournal.created_on_init(j.path)
        cards = reloaded.cards()
        assert len(cards) == 1
        assert cards[0].qty == 2
        assert cards[0].status == "ОТКРЫТА"


class TestJournalMalformedRows:
    def test_broken_rows_are_skipped(self, tmp_path):
        header = ",".join(COLUMNS_RU)
        path = tmp_path / "j.csv"
        path.write_text(
            header + "\n"
            "1,ЗАЯВКА,2026-09-14 13:00:00,1,NG-1,NG-10.26,BUY,2,100.0,98.0,,,,,2.0,2000.0,5000.0,,tf=1h\n"
            "broken,line,unparseable,,,,,,,,,,,,,,,,\n"
            "4,НЕВЕДОМО,2026-09-14 13:00:00,4,NG-1,NG-10.26,BUY,1,1.0,,,,,,,2.0,2000.0,5000.0,,\n"
            "6,ВХОД,2026-09-14 13:00:00,1,NG-1,NG-10.26,BUY,2,100.0,98.0,,,,,2.0,2000.0,5000.0,,tf=1h\n",
            encoding="utf-8-sig",
        )
        j = TradeJournal(path, next_event_id=1)
        events = j.events()
        assert len(events) == 2
        assert events[0].op == "ЗАЯВКА"
        assert events[1].op == "ВХОД"

    def test_replay_from_russian_header_roundtrip(self, tmp_path):
        j = _journal(tmp_path)
        pid = make_position_id("NG")
        j.append(_ev(j, id=j.next_id, op="ВХОД", position_id=pid, qty="2", price="100.0"))

        reloaded = TradeJournal.created_on_init(j.path)
        state = reloaded.replay(initial_deposit=100000)

        assert state.positions[pid].qty == 2
        assert state.positions[pid].avg_price == 100.0

    def test_unknown_op_rejected_on_append(self, tmp_path):
        j = _journal(tmp_path)
        with pytest.raises(ValueError, match="операции"):
            j.append(_ev(j, id=j.next_id, op="PENDING"))

    def test_non_monotonic_id_rejected(self, tmp_path):
        j = _journal(tmp_path)
        j.append(_ev(j, id=j.next_id))
        with pytest.raises(ValueError):
            j.append(_ev(j, id=1))

    def test_legacy_header_regenerated(self, tmp_path):
        path = tmp_path / "j.csv"
        path.write_text(
            "id,position_id,Статус,Время заявки,Дата,Сторона,Время входа,Депозит,Риск %,Риск руб,"
            "Цена входа,Цена стопа,Кол-во,Цена выхода,Прибыль руб,Комиссия руб,Шаг цены,Стоимость шага,"
            "ГО (покупка),ГО (продажа),Контракт,Причина,Заметки\n"
            "1,NG-1,NEW,2026-09-14 13:00:00,,BUY,,,,100.0,98.0,2,,,,,,,,,NG,,tf=1h\n",
            encoding="utf-8-sig",
        )
        j = TradeJournal.created_on_init(path)
        header = path.read_text(encoding="utf-8-sig").splitlines()[0]
        assert header.split(",") == COLUMNS_RU
        assert j.events() == []
        assert j.next_id == 1