"""Алгоритм вердикта clusters-v1 (change add-cluster-filter, design D5)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.clusters.settings import ClusterSettings
from src.clusters.verdict import evaluate, sigma15
from tests.unit.clusters.helpers import BR_CLOSES, DAY, SIGMA, T, br_events, candles, event, history, signal_day

SETTINGS = ClusterSettings()


def _no_initiative():
    """События BR без продажи 13:37 — единственной инициативы по направлению SELL."""
    return [e for e in br_events() if not (e.event_type == "large_aggressor" and e.direction == "short")]


def _evaluate(side=-1, frame=None, events=None, revoked=frozenset(), mode="shadow", bar_close=T, settings=SETTINGS):
    return evaluate(side=side, bar_close=bar_close, candles=frame if frame is not None else candles(history(), signal_day()),
                    events=events if events is not None else br_events(), revoked=set(revoked),
                    settings=settings, mode=mode)


class TestSigma:
    def test_median_of_fifteen_minute_moves_over_previous_days(self):
        sigma, days, values = sigma15(candles(history(), signal_day()), DAY, 10)

        assert sigma == pytest.approx(SIGMA)
        assert days == 10
        assert values == 10 * (8 * 60 - 15)

    def test_signal_day_is_not_part_of_sigma(self):
        frame = candles(history(), signal_day({"13:49": 200.0}))

        assert sigma15(frame, DAY, 10)[0] == pytest.approx(SIGMA)


class TestBrExample:
    """BR-11.26 SELL 15.10.2026, T = 13:50 UTC: запрет на кульминации."""

    def test_veto_on_climax(self):
        verdict = _evaluate()

        assert verdict.verdict == "veto"
        assert verdict.reason == "climax"
        assert verdict.inputs["sigma15_bps"] == pytest.approx(12.82)
        assert verdict.inputs["pre_move_sigma"] == pytest.approx(2.159, abs=1e-3)
        assert verdict.inputs["range_position"] == pytest.approx(0.5, abs=1e-3)
        assert verdict.inputs["close_at_entry"]["close"] == 104.63
        assert verdict.inputs["close_15m_before"]["close"] == 104.92

    def test_event_labels(self):
        labels = [(item.event_type, item.relation, item.label, round(item.own, 2)) for item in _evaluate().evidence]

        assert labels == [
            ("large_aggressor", "opposite", "climax", 3.28),
            ("stacked_imbalance", "opposite", "climax", 3.27),
            ("large_aggressor", "same", "initiative", 0.07),
            ("volume_spike", "same", "climax", 2.45),
        ]

    def test_shadow_message(self):
        assert _evaluate().message == (
            "запретил бы — вход на кульминации · ход до входа +2,16σ (порог 1,5σ) · "
            "середина 2ч диапазона · 16:40 всплеск продаж ×9,6 после хода +2,45σ")

    def test_enforce_message_has_no_conditional(self):
        assert _evaluate(mode="enforce").message.startswith("вход на кульминации · ход до входа +2,16σ")


class TestRule:
    def test_strong_veto_at_range_edge(self):
        closes = dict(BR_CLOSES, **{"13:49": 104.10})  # SELL у минимума диапазона
        verdict = _evaluate(frame=candles(history(), signal_day(closes)))

        assert verdict.verdict == "veto_strong"
        assert "у края 2ч диапазона" in verdict.message

    def test_confirm_on_initiative_before_move(self):
        utc = timezone.utc
        closes = {"13:34": 104.40, "13:49": 104.80, "13:24": 104.90, "13:09": 105.05, "13:36": 104.90, "13:21": 105.0}
        events = [event("large_aggressor", "long", datetime(2026, 10, 15, 13, 25, 0, tzinfo=utc),
                        payload={"side": "BUY", "size": 1000, "size_unit": "contracts"})]
        verdict = _evaluate(side=1, frame=candles(history(), signal_day(closes, base=104.6)), events=events)

        assert verdict.verdict == "confirm"
        assert verdict.reason == "initiative"
        assert verdict.message.startswith("подтверждает — инициатива до движения · 16:25 крупная покупка 1 000 контр.")

    def test_move_below_threshold_is_neutral(self):
        closes = dict(BR_CLOSES, **{"13:34": 104.70})  # ход до входа меньше 1,5σ
        verdict = _evaluate(frame=candles(history(), signal_day(closes)), events=_no_initiative())

        assert verdict.verdict == "neutral"
        assert verdict.reason == "no_evidence"

    def test_initiative_confirms_when_no_veto(self):
        closes = dict(BR_CLOSES, **{"13:34": 104.70})
        verdict = _evaluate(frame=candles(history(), signal_day(closes)))

        assert verdict.verdict == "confirm"
        assert "16:37 крупная продажа 650 контр." in verdict.message

    def test_absorption_against_entry_vetoes(self):
        events = [event("absorption", "long", datetime(2026, 10, 15, 13, 45, tzinfo=timezone.utc),
                        payload={"absorbed_side": "SELL"})]

        verdict = _evaluate(events=events)

        assert verdict.verdict == "veto"
        assert verdict.evidence[0].label == "absorption_against"
        assert "поглощение продаж" in verdict.message

    def test_climax_older_than_window_does_not_veto(self):
        settings = ClusterSettings(climax_window_min=5)

        assert _evaluate(settings=settings, events=_no_initiative()).verdict == "neutral"

    def test_event_after_bar_close_is_ignored(self):
        late = event("volume_spike", "short", T + timedelta(seconds=1), payload={"ratio": 20.0})
        events = [e for e in _no_initiative() if e.event_type != "volume_spike"] + [late]

        assert _evaluate(events=events).verdict == "neutral"

    def test_revoked_before_bar_is_ignored(self):
        spike = br_events()[-1]

        assert _evaluate(events=_no_initiative(), revoked={spike.signal_id}).verdict == "neutral"

    def test_neutral_direction_is_skipped(self):
        events = [event("volume_spike", "neutral", datetime(2026, 10, 15, 13, 40, tzinfo=timezone.utc))]

        verdict = _evaluate(events=events)

        assert verdict.evidence == ()
        assert verdict.verdict == "neutral"


class TestUnknown:
    def test_short_history(self):
        verdict = _evaluate(frame=candles(history(days=4), signal_day()))

        assert (verdict.verdict, verdict.reason) == ("unknown", "no_sigma_history")
        assert verdict.message == "нет данных — мало 1m истории для σ (4 из 10 дней)"

    def test_no_candles_near_signal(self):
        verdict = _evaluate(frame=candles(history()))

        assert (verdict.verdict, verdict.reason) == ("unknown", "candles_missing")

    def test_unknown_never_blocks(self):
        verdict = _evaluate(frame=candles(history(days=4), signal_day()), mode="enforce")

        assert not verdict.blocks("veto")


class TestBlocks:
    def test_enforce_level(self):
        veto = _evaluate(mode="enforce")

        assert veto.blocks("veto")
        assert not veto.blocks("veto_strong")

    def test_shadow_never_blocks(self):
        assert not _evaluate(mode="shadow").blocks("veto")
