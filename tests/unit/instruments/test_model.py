import pytest

from src.instruments import Instrument, normalize_instrument


class TestNormalizeInstrument:
    def test_three_tuple(self):
        inst = normalize_instrument(("NG (Природный газ) — NG-9.26", "NGU6", "future"))
        assert inst.label == "NG (Природный газ) — NG-9.26"
        assert inst.ticker == "NGU6"
        assert inst.instrument_type == "future"

    def test_two_tuple_fallback_label(self):
        inst = normalize_instrument(("GAZP", "share"))
        assert inst.ticker == "GAZP"
        assert inst.instrument_type == "share"
        assert inst.label == "GAZP share"

    def test_instrument_is_frozen_dataclass(self):
        inst = normalize_instrument(("SBER", "SBER", "share"))
        assert isinstance(inst, Instrument)
        with pytest.raises(Exception):
            inst.label = "changed"

    def test_unknown_size_raises(self):
        with pytest.raises(ValueError, match="Некорректный инструмент"):
            normalize_instrument(("SBER",))
