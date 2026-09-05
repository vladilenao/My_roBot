import math

from src.market_structure.fibonacci import (
    extension_level,
    in_zone,
    retracement_level,
)


class TestRetracementLevel:
    def test_downward_wave_retracement(self):
        # X=100 (high) -> A=80 (low), retracement 61.8% up = 80 + 0.618*20
        assert math.isclose(retracement_level(a=80.0, x=100.0, ratio=0.618), 92.36)
        assert math.isclose(retracement_level(a=80.0, x=100.0, ratio=0.382), 87.64)

    def test_upward_wave_retracement_mirror(self):
        # X=80 (low) -> A=100 (high), retracement 61.8% down = 100 - 0.618*20
        assert math.isclose(retracement_level(a=100.0, x=80.0, ratio=0.618), 87.64)
        assert math.isclose(retracement_level(a=100.0, x=80.0, ratio=0.382), 92.36)


class TestExtensionLevel:
    def test_downward_wave_extension(self):
        # X=100 (high) -> A=80 (low), D = 80 + 1.618*20
        assert math.isclose(extension_level(a=80.0, x=100.0), 112.36)

    def test_upward_wave_extension_mirror(self):
        # X=80 (low) -> A=100 (high), D = 100 - 1.618*20
        assert math.isclose(extension_level(a=100.0, x=80.0), 67.64)


class TestInZone:
    def test_inside_zone(self):
        assert in_zone(price=90.9, level=90.0, amplitude=200.0, tolerance=0.02) is True

    def test_outside_zone(self):
        assert in_zone(price=85.0, level=90.0, amplitude=200.0, tolerance=0.02) is False

    def test_boundary_inside(self):
        # нижняя граница: 90*(1-0.02) = 88.2
        assert in_zone(price=88.2, level=90.0, amplitude=200.0, tolerance=0.02) is True
        # верхняя граница: 90*(1+0.02) = 91.8
        assert in_zone(price=91.8, level=90.0, amplitude=200.0, tolerance=0.02) is True

    def test_zero_amplitude(self):
        assert in_zone(price=90.0, level=90.0, amplitude=0.0, tolerance=0.02) is True
        assert in_zone(price=91.0, level=90.0, amplitude=0.0, tolerance=0.02) is False