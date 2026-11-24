"""Порт брокера (refactor-broker-port): граница общего кода и протоколы адаптера Т-Инвест."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.broker_contour import port
from src.broker_contour.prices import PriceBook
from src.broker_contour.tinvest.port import TInvestPort
from tests.unit.broker_contour.fake_api import FakeApi

PACKAGE = Path(__file__).resolve().parents[3] / "src" / "broker_contour"
COMMON = ("broker", "stops", "stream", "process", "admission", "prices", "orders", "reconcile", "guards", "control",
          "notify", "fingerprint", "store", "bindings", "port", "contours", "__init__")
FORBIDDEN = ("t_tech", "grpc", "src.sandbox", "src.account", "src.broker_contour.tinvest", "src.broker_contour.finam")


def _imports(path: Path) -> set[str]:
    """Все импорты модуля, в том числе внутри функций; относительные — от пакета ``src.broker_contour``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = "src.broker_contour" + (f".{base}" if base else "")
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("name", COMMON)
def test_common_code_knows_no_sdk_and_no_adapter(name):
    path = PACKAGE / f"{name}.py"
    assert path.exists(), f"нет модуля общего кода {name}"
    bad = sorted(item for item in _imports(path) for prefix in FORBIDDEN
                 if item == prefix or item.startswith(prefix + "."))
    assert bad == [], f"{name}.py импортирует {bad}"


def test_every_common_module_is_checked():
    modules = {path.stem for path in PACKAGE.glob("*.py")}
    assert modules == set(COMMON), "новый модуль общего кода — добавьте его в COMMON или перенесите в адаптер"


@pytest.mark.parametrize("protocol", [port.OrderPort, port.StopPort, port.MarketPort, port.OrderFeed,
                                      port.MarginLimits])
def test_tinvest_port_implements_ports(protocol):
    assert isinstance(TInvestPort(FakeApi(), PriceBook()), protocol)


def test_session_implements_broker_session():
    from src.broker_contour.tinvest.session import TInvestSession

    session = TInvestSession(object(), rule=lambda gateway: None)
    assert isinstance(session, port.BrokerSession)
