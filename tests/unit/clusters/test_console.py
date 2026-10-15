"""Строка «Кластеры» в консоли (change add-cluster-filter, design D7)."""

from datetime import datetime, timezone

from src.events.event import Event
from src.notifier.templates import render

NOW = datetime(2026, 10, 15, 13, 50, tzinfo=timezone.utc)
VETO = ("запретил бы — вход на кульминации · ход до входа +2,16σ (порог 1,5σ) · "
        "середина 2ч диапазона · 16:40 всплеск продаж ×9,6 после хода +2,45σ")


def _plan(clusters=None):
    return Event.signal("BR-11.26", side="SELL", quantity=4, entry="104.63", stop="105.06",
                        targets=("104.19", "103.76"), timeframe="5m", bar_time=NOW, clusters=clusters)


def _lines(event):
    return render(event, now=NOW, linked=True).splitlines()


def test_shadow_veto_is_first_plan_detail():
    lines = _lines(_plan({"verdict": "veto", "reason": "climax", "message": VETO}))

    assert lines[1].strip().startswith("Кластеры: запретил бы — вход на кульминации")
    assert "16:40 всплеск продаж ×9,6 после хода +2,45σ" in " ".join(line.strip() for line in lines)
    assert any(line.strip().startswith("Цели:") for line in lines[2:])


def test_neutral_prints_nothing():
    neutral = _plan({"verdict": "neutral", "reason": "no_evidence", "message": "без признаков"})

    assert render(neutral, now=NOW) == render(_plan(), now=NOW)
    assert "Кластеры" not in render(neutral, now=NOW)


def test_unknown_names_short_contracts_only():
    text = render(_plan({"verdict": "unknown", "reason": "contract_mismatch",
                         "message": "нет данных — коллектор ведёт NG-10.26, робот торгует NG-11.26"}), now=NOW)

    assert "Кластеры: нет данных — коллектор ведёт NG-10.26, робот торгует NG-11.26" in text
    assert "NGV6" not in text and "NGX6" not in text


def test_enforce_rejection():
    rejected = Event.rejected("BR-11.26", reason="Кластеры: вход на кульминации", code="cluster-climax",
                              side="SELL", timeframe="5m", bar_time=NOW,
                              clusters={"verdict": "veto", "reason": "climax",
                                        "message": "вход на кульминации · ход до входа +2,16σ (порог 1,5σ)"})

    lines = render(rejected, now=NOW, linked=True).splitlines()

    assert "ОТКЛОНЕНО" in lines[0] and "Кластеры: вход на кульминации" in lines[0]
    assert lines[1].strip() == "Кластеры: вход на кульминации · ход до входа +2,16σ (порог 1,5σ)"


def test_trend_rejection_with_shadow_confirm():
    rejected = Event.rejected("BR-11.26", reason="покупка против нисходящего тренда", code="filter-rejected",
                              side="BUY", clusters={"verdict": "confirm", "reason": "initiative",
                                                    "message": "подтверждает — инициатива до движения"})

    lines = render(rejected, now=NOW, linked=True).splitlines()

    assert "Покупка против нисходящего тренда" in lines[0]
    assert lines[1].strip() == "Кластеры: подтверждает — инициатива до движения"
