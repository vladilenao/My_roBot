import pytest

from src.instruments import Instrument, normalize_instrument


class TestNormalizeInstrument:
    def test_three_tuple(self):
        inst = normalize_instrument(("NG (Природный газ) — NG-9.26", "NGU6", "future"))
        assert inst.label == "NG (Природный газ) — NG-9.26"
        assert inst.ticker == "NGU6"
        assert inst.instrument_type == "future"
        assert inst.short_name == "NGU6"

    def test_four_tuple(self):
        inst = normalize_instrument(("NG (Природный газ) — NG-9.26", "NGU6", "future", "NG-9.26"))
        assert inst.label == "NG (Природный газ) — NG-9.26"
        assert inst.ticker == "NGU6"
        assert inst.instrument_type == "future"
        assert inst.short_name == "NG-9.26"

    def test_four_tuple_empty_short_name_falls_back_to_ticker(self):
        inst = normalize_instrument(("SBER", "SBER", "share", None))
        assert inst.short_name == "SBER"

    def test_two_tuple_fallback_label(self):
        inst = normalize_instrument(("GAZP", "share"))
        assert inst.ticker == "GAZP"
        assert inst.instrument_type == "share"
        assert inst.label == "GAZP share"
        assert inst.short_name == "GAZP"

    def test_instrument_is_frozen_dataclass(self):
        inst = normalize_instrument(("SBER", "SBER", "share"))
        assert isinstance(inst, Instrument)
        with pytest.raises(Exception):
            inst.label = "changed"

    def test_unknown_size_raises(self):
        with pytest.raises(ValueError, match="Некорректный инструмент"):
            normalize_instrument(("SBER",))
