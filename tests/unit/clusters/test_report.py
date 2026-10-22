"""Отчёт GET /api/v1/clusters/report (change add-cluster-filter, design D9)."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib import error, request

import pandas as pd
import pytest
import yaml

from src.clusters.report import ClusterReport, ReportError
from src.data.export_api import MarketDataExportServer
from src.data.market_store import MarketDataStore
from src.trade_journal.storage import Storage
from src.trade_management.audit import CalculationTraceRepository
from tests.unit.clusters.helpers import T
from tests.unit.clusters.test_gate_admission import BR, _assignment, _decision, _gate, _store

SPEC = yaml.safe_load((Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
SIGNAL_ID = "future-br-macd:BRX6:5m:SELL"


def _check(instance, schema, path="report"):
    """Мини-проверка ответа по схеме OpenAPI 3.0: $ref, allOf, nullable, типы, required, enum."""
    if "$ref" in schema:
        schema = SPEC["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    if instance is None:
        assert schema.get("nullable"), f"{path}: null не разрешён"
        return
    for part in schema.get("allOf", ()):
        _check(instance, part, path)
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "number": (int, float)}
    if kind:
        assert isinstance(instance, types[kind]), f"{path}: ожидалось {kind}, получено {type(instance).__name__}"
    if "enum" in schema:
        assert instance in schema["enum"], f"{path}: {instance!r} вне {schema['enum']}"
    if kind == "object":
        for name in schema.get("required", ()):
            assert name in instance, f"{path}: нет поля {name!r}"
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, value in instance.items():
            if name in properties:
                _check(value, properties[name], f"{path}.{name}")
            elif extra is False:
                raise AssertionError(f"{path}: поле вне схемы {name!r}")
            elif isinstance(extra, dict):
                _check(value, extra, f"{path}.{name}")
    if kind == "array" and "items" in schema:
        for index, item in enumerate(instance):
            _check(item, schema["items"], f"{path}[{index}]")


def _after_entry():
    """1m свечи после входа: закрытия 14:04 и 14:49 UTC — как у BR-11.26 15.10."""
    rows = [{"datetime": datetime(2026, 10, 15, 13, 49) + timedelta(minutes=m), "close": 104.63} for m in range(70)]
    rows[15]["close"] = 105.04
    rows[60]["close"] = 105.41
    frame = pd.DataFrame(rows)
    return lambda uid: frame if uid == "b2d0f863" else pd.DataFrame(columns=["datetime", "close"])


def _trades_db(tmp_path, *, pnl="-1455.94", with_trade=True):
    path = tmp_path / "trades.sqlite3"
    with Storage(path) as storage:
        gate = _gate(_store(tmp_path))
        decision = _decision()
        verdict = gate.evaluate(_assignment(), decision, BR)
        CalculationTraceRepository(storage).record(gate.trace(verdict, _assignment(), decision, BR, "5m", rejected=False))
        if with_trade:
            conn = storage.connection
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO trades (trade_id, assignment_id, instrument_id, signal_id, side, plan_json, "
                         "profile_json, phase, created_at, updated_at) VALUES (?, 'future-br-macd', 'BRX6', ?, 'SELL', "
                         "'{}', '{}', 'CLOSED', '2026-10-15T13:50:00', '2026-10-15T14:04:00')", (SIGNAL_ID, SIGNAL_ID))
            conn.execute("INSERT INTO fills (fill_id, order_id, trade_id, command_id, execution_id, quantity, price, "
                         "executed_at) VALUES ('f1', 'o1', ?, 'c1', 'e1', 4, '104.64', '2026-10-15T13:51:00Z')", (SIGNAL_ID,))
            conn.execute("INSERT INTO positions (trade_id, side, quantity, net_realized_pnl, updated_at) "
                         "VALUES (?, 'SELL', 0, ?, '2026-10-15T14:04:00')", (SIGNAL_ID, pnl))
            conn.commit()
    return path


def _report(tmp_path, now=T + timedelta(hours=2), **kwargs):
    return ClusterReport(_trades_db(tmp_path, **kwargs), _after_entry(), configured_mode="shadow", clock=lambda: now)


class TestReport:
    def test_day_with_shadow_veto(self, tmp_path):
        payload = _report(tmp_path).build(date(2026, 10, 15), 1)

        _check(payload, {"$ref": "#/components/schemas/ClustersReport"})
        group, = payload["groups"]
        assert (group["verdict"], group["signals"], group["trades"]) == ("veto", 1, 1)
        assert group["trades_net_pnl"] == {"amount": "-1455.94", "currency": "RUB"}
        assert (group["trades_won"], group["trades_lost"]) == (0, 1)
        assert group["price_move"]["60"] == {"n": 1, "mean_bps": "-74.27", "median_bps": "-74.27", "hit_rate": "0.00"}
        assert payload["veto_effect"]["net_effect"] == {"amount": "1455.94", "currency": "RUB"}
        signal, = payload["signals"]
        assert signal["price_move"] == {"15": {"status": "final", "move_bps": "-39.11"},
                                        "60": {"status": "final", "move_bps": "-74.27"}}
        assert signal["message"].startswith("запретил бы — вход на кульминации")
        assert payload["mode"] == "shadow"

    def test_period_and_links(self, tmp_path):
        report = _report(tmp_path, now=datetime(2026, 11, 5, 20, 0, tzinfo=T.tzinfo))

        payload = report.build(date(2026, 11, 5), 22)

        assert payload["period"] == {"from": "2026-10-15", "to": "2026-11-05", "days": 22}
        assert payload["_links"]["previous"]["href"] == "/api/v1/clusters/report?date=2026-10-14&period=22"
        assert "next" not in payload["_links"]
        assert len(payload["signals"]) == 1

    def test_next_link_for_past_period(self, tmp_path):
        payload = _report(tmp_path, now=datetime(2026, 10, 20, 12, 0, tzinfo=T.tzinfo)).build(date(2026, 10, 15), 1)

        assert payload["_links"]["next"]["href"] == "/api/v1/clusters/report?date=2026-10-16&period=1"

    def test_horizon_not_reached_is_pending(self, tmp_path):
        payload = _report(tmp_path, now=T + timedelta(minutes=20)).build(date(2026, 10, 15), 1)

        assert payload["signals"][0]["price_move"]["15"]["status"] == "final"
        assert payload["signals"][0]["price_move"]["60"] == {"status": "pending", "move_bps": None}
        assert payload["groups"][0]["price_move"]["60"]["n"] == 0

    def test_empty_period(self, tmp_path):
        payload = _report(tmp_path).build(date(2026, 10, 14), 1)

        assert payload["groups"] == [] and payload["signals"] == []
        _check(payload, {"$ref": "#/components/schemas/ClustersReport"})

    def test_signal_without_trade(self, tmp_path):
        payload = _report(tmp_path, with_trade=False).build(date(2026, 10, 15), 1)

        assert payload["signals"][0]["trade"] is None
        assert payload["groups"][0]["trades"] == 0
        assert payload["veto_effect"]["trades_removed"] == 0

    @pytest.mark.parametrize("raw_date, raw_period", [(None, None), ("15.10.2026", None), ("2026-10-15", "0"),
                                                       ("2026-10-15", "x"), ("2026-12-01", None)])
    def test_bad_parameters(self, tmp_path, raw_date, raw_period):
        with pytest.raises(ReportError):
            _report(tmp_path).parse(raw_date, raw_period)


def _get(base, path, token="secret"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with request.urlopen(request.Request(base + path, headers=headers), timeout=5) as response:
        return response.status, response.headers.get("Content-Type"), json.loads(response.read())


def test_rest_route(tmp_path):
    server = MarketDataExportServer(MarketDataStore(tmp_path / "market-data.sqlite3"), "secret", port=0,
                                    clusters_report=_report(tmp_path))
    try:
        server.start()
    except PermissionError:
        pytest.skip("Песочница не разрешает открыть loopback-сокет")
    try:
        host, port = server.address
        base = f"http://{host}:{port}"
        status, content_type, payload = _get(base, "/api/v1/clusters/report?date=2026-10-15")

        assert status == 200 and content_type.startswith("application/hal+json")
        assert payload["groups"][0]["verdict"] == "veto"
        with pytest.raises(error.HTTPError) as bad:
            _get(base, "/api/v1/clusters/report?date=2026-10-15&period=0")
        assert bad.value.code == 400
        assert json.loads(bad.value.read())["code"] == "BAD_PERIOD"
        with pytest.raises(error.HTTPError) as anonymous:
            _get(base, "/api/v1/clusters/report?date=2026-10-15", token=None)
        assert anonymous.value.code == 401
        with request.urlopen(base + "/openapi.yaml", timeout=5) as response:
            assert "/api/v1/clusters/report" in response.read().decode("utf-8")
    finally:
        server.close()


def test_local_api_without_token_does_not_start(tmp_path, monkeypatch, capsys):
    import run

    monkeypatch.setattr(run, "MARKET_DATA_EXPORT_TOKEN", None)
    monkeypatch.setattr(run, "ROBOT_CONTROL_TOKEN", None)

    assert run._start_local_api(MarketDataStore(tmp_path / "market-data.sqlite3"), tmp_path) is None
    assert "MARKET_DATA_EXPORT_TOKEN" in capsys.readouterr().out
