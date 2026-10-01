"""Рублёвые метрики отклонений и результата.

MAE и MFE отвечают на вопрос «насколько глубоко ходил рынок, пока позиция была
открыта», а результат — «во сколько раз вложенный риск превратился в деньги».
Обе величины бессмысленны в голых пунктах цены: пункт у одного контракта стоит
одного, у другого — другого. Поэтому и числитель, и знаменатель считаются в
рублях через шаг контракта и объём позиции.
"""

import json
import logging

from src.trade_journal.storage import Storage


def _write_trade(
    connection, trade_id, *, instrument="NG", price_step="1", step_cost="100",
    reference_entry="100", stop_price="96", low="97", high="103", realized="-450",
):
    started, finished = "2026-09-18T07:00:00+00:00", "2026-09-18T07:45:00+00:00"
    cid, order_id, fill_id, event_id = (f"c-{trade_id}", f"o-{trade_id}", f"f-{trade_id}", f"e-{trade_id}")
    factors = "NULL, NULL" if price_step is None else f"'{price_step}', '{step_cost}'"
    connection.execute(
        "INSERT INTO trades (trade_id, assignment_id, instrument_id, signal_id, side, plan_json, "
        f"profile_json, phase, state_revision, profile_state_json, created_at, updated_at, price_step, step_cost) "
        f"VALUES ('{trade_id}', 'a', '{instrument}', 's', 'BUY', "
        f"'{{\"reference_entry\": \"{reference_entry}\", \"stop_price\": \"{stop_price}\"}}', '{{}}', "
        f"'CLOSED', 0, '{{}}', '{started}', '{finished}', {factors})"
    )
    connection.execute(
        "INSERT INTO positions VALUES (?, 'BUY', 0, NULL, ?, '0', ?, ?)",
        (trade_id, realized, realized, finished),
    )
    connection.execute(
        "INSERT INTO outbox VALUES (?, ?, '{}', 'SENT', ?, NULL)", (cid, trade_id, started)
    )
    connection.execute(
        "INSERT INTO orders VALUES (?, ?, ?, 'OPEN', 'FILLED', 2, 2, ?, ?, ?)",
        (order_id, trade_id, cid, reference_entry, started, finished),
    )
    connection.execute(
        "INSERT INTO fills VALUES (?, ?, ?, ?, ?, 2, ?, '0', ?)",
        (fill_id, order_id, trade_id, cid, event_id, reference_entry, started),
    )
    connection.execute(
        "INSERT INTO events VALUES (NULL, ?, ?, ?, ?, 'FILL', ?, ?)",
        (event_id, trade_id, order_id, cid, json.dumps({"price": reference_entry, "low": low, "high": high}), started),
    )
    connection.execute(
        "INSERT INTO reservations VALUES (?, ?, ?, '500', '0', '500', '0', 'RELEASED', ?, ?)",
        (f"res-{trade_id}", trade_id, order_id, started, finished),
    )


def _card(tmp_path, build):
    summary = tmp_path / "trade_summary.csv"
    with Storage(tmp_path / "trades.sqlite3", journal_path=tmp_path / "e.csv",
                 positions_path=summary) as storage:
        with storage.transaction() as connection:
            build(connection)
    with summary.open(newline="", encoding="utf-8") as handle:
        import csv
        return next(csv.DictReader(handle))


def test_excursions_are_priced_through_the_contract_step(tmp_path):
    """Отклонение в 3 пункта на контракте со стоимостью шага 100 рублей — это 300 рублей."""
    row = _card(tmp_path, lambda c: _write_trade(c, "t1"))

    # initial risk: 4 points * 100 RUB per point * 2 contracts
    assert row["Initial Risk"] == "800.00"
    assert row["MAE"] == "0.75"   # 3 points against a 4-point risk
    assert row["MFE"] == "0.75"   # 3 points against a 4-point risk


def test_two_contracts_with_different_steps_give_comparable_r(tmp_path):
    """Один и тот же ход в пунктах у контрактов с разным шагом не сравним, а в рублях — да."""
    def build(connection):
        _write_trade(connection, "cheap", instrument="CHEAP", price_step="1", step_cost="10")
        _write_trade(connection, "rich", instrument="RICH", price_step="50", step_cost="5000")

    import csv
    summary = tmp_path / "trade_summary.csv"
    with Storage(tmp_path / "trades.sqlite3", journal_path=tmp_path / "e.csv",
                 positions_path=summary) as storage:
        with storage.transaction() as connection:
            build(connection)
    with summary.open(newline="", encoding="utf-8") as handle:
        by_id = {r["Trade ID"]: r for r in csv.DictReader(handle)}

    # both moved 3 points on a 4-point risk, on the same 2 contracts
    assert by_id["cheap"]["MAE"] == "0.75"
    assert by_id["rich"]["MAE"] == "0.75"
    assert by_id["cheap"]["Initial Risk"] != by_id["rich"]["Initial Risk"]


def test_peak_size_scales_the_risk_denominator(tmp_path):
    """Знаменатель берётся по максимальному объёму, а не по первоначальному."""
    def build(connection):
        _write_trade(connection, "t1")
        connection.execute("UPDATE trades SET plan_json = "
                           "'{\"reference_entry\": \"100\", \"stop_price\": \"96\"}' WHERE trade_id='t1'")
        connection.execute("INSERT INTO outbox VALUES ('c2', 't1', '{}', 'SENT', "
                           "'2026-09-18T07:10:00+00:00', NULL)")
        connection.execute("INSERT INTO orders VALUES ('o2', 't1', 'c2', 'ADD', 'FILLED', 3, 3, '100', "
                           "'2026-09-18T07:10:00+00:00', '2026-09-18T07:10:00+00:00')")
        connection.execute("INSERT INTO fills VALUES ('f2', 'o2', 't1', 'c2', 'e2', 3, '100', '0', "
                           "'2026-09-18T07:10:00+00:00')")
        connection.execute("INSERT INTO events VALUES (NULL, 'e2', 't1', 'o2', 'c2', 'FILL', "
                           "'{\"price\": \"100\", \"low\": \"97\", \"high\": \"103\"}', "
                           "'2026-09-18T07:10:00+00:00')")

    row = _card(tmp_path, build)

    assert row["Плановый риск"] == "500.00"     # unchanged reservation
    assert row["Initial Risk"] == "2000.00"     # 4 points * 100 RUB * 5 contracts
    assert row["MAE"] == "0.75"   # the peak scales both sides


def test_a_bar_without_an_execution_is_not_counted(tmp_path):
    """Экстремум бара, на котором ничего не исполнилось, ничего не говорит о позиции."""
    def build(connection):
        _write_trade(connection, "t1", low="97", high="103")
        connection.execute(
            "INSERT INTO events VALUES (NULL, 'e-ack', 't1', 'o-t1', 'c-t1', 'ACK', "
            "'{\"price\": \"100\", \"low\": \"50\", \"high\": \"500\"}', "
            "'2026-09-18T07:10:00+00:00')"
        )

    row = _card(tmp_path, build)

    # only the bar the fill happened on counts, so MAE stays at 3 points
    assert row["MAE"] == "0.75"


def test_trade_without_step_is_marked_unusable(tmp_path):
    """Без стоимости шага рублёвые метрики не выдумываются."""
    row = _card(tmp_path, lambda c: _write_trade(c, "t1", price_step=None))

    assert row["Ед. PnL"] == "RAW"
    assert row["Initial Risk"] == ""
    assert row["Result"] == ""
    assert row["MAE"] == ""
    assert row["MFE"] == ""
    assert row["Плановый риск"] == "500.00"


def test_missing_step_warns_once_per_trade_and_reads_no_metadata(monkeypatch, caplog):
    """Нехватка шага — повод предупредить ровно один раз, а не сходить за данными."""
    import src.instruments.selector as selector
    from src.trade_journal import reducer

    def explode(*args, **kwargs):
        raise AssertionError("редьюсер обратился к метаданным контракта")

    monkeypatch.setattr(selector, "select_instruments", explode)
    monkeypatch.setattr(reducer, "_WARNED_STEP_MISSING", set())

    with caplog.at_level(logging.WARNING, logger=reducer.__name__):
        for _ in range(3):
            reducer._warn_missing_step("t1")
        reducer._warn_missing_step("t2")

    assert caplog.text.count("t1") == 1
    assert caplog.text.count("t2") == 1


def test_result_r_uses_the_realized_risk(tmp_path):
    """Результат делится на фактический риск, а не на резерв допуска."""
    row = _card(tmp_path, lambda c: _write_trade(c, "t1", realized="-400"))

    # the 500 RUB reservation is still reported separately and is not the denominator
    assert row["Плановый риск"] == "500.00"
    assert row["Initial Risk"] == "800.00"
    assert row["Result"] == "-0.50"