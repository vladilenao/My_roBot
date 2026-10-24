"""Даты и диапазон прогона истории из команды API (прежде — из диалога консоли)."""

from datetime import datetime

import pytest

from src.bot import launch


class TestParseMoment:
    def test_naive_input_is_utc(self):
        assert launch.parse_moment("2023-06-01 10:00") == datetime(2023, 6, 1, 10, 0)

    def test_explicit_zone_converted_to_utc(self):
        assert launch.parse_moment("2023-06-01T13:00:00+03:00") == datetime(2023, 6, 1, 10, 0)

    def test_z_suffix_parsed(self):
        assert launch.parse_moment("2023-06-01T10:00:00Z") == datetime(2023, 6, 1, 10, 0)

    def test_date_only_means_midnight(self):
        assert launch.parse_moment("2023-06-01") == datetime(2023, 6, 1, 0, 0)

    def test_surrounding_spaces_ignored(self):
        assert launch.parse_moment("  2023-06-01 10:00 ") == datetime(2023, 6, 1, 10, 0)

    @pytest.mark.parametrize("raw", ["", "   ", "01.06.2023", "завтра", "2023-13-01 10:00"])
    def test_unparsable_input_raises(self, raw):
        with pytest.raises(ValueError):
            launch.parse_moment(raw)

    def test_error_message_shows_expected_format(self):
        with pytest.raises(ValueError) as exc:
            launch.parse_moment("01.06.2023")
        assert "ГГГГ-ММ-ДД ЧЧ:ММ" in str(exc.value)


class TestValidateRange:
    def test_valid_range_passes_through(self):
        start, end = datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 12)
        assert launch.validate_range(start, end, 0.0) == (start, end, 0.0)

    def test_equal_moments_rejected(self):
        moment = datetime(2023, 6, 1, 10)
        with pytest.raises(launch.RunConfigurationError):
            launch.validate_range(moment, moment, 1.0)

    def test_reversed_range_rejected(self):
        with pytest.raises(launch.RunConfigurationError):
            launch.validate_range(datetime(2023, 6, 1, 12), datetime(2023, 6, 1, 10), 1.0)

    def test_timezone_aware_input_normalized_to_naive(self):
        from datetime import timezone

        start = datetime(2023, 6, 1, 13, tzinfo=timezone.utc)
        end = datetime(2023, 6, 1, 16, tzinfo=timezone.utc)
        got = launch.validate_range(start, end, 2)

        assert got == (datetime(2023, 6, 1, 13), datetime(2023, 6, 1, 16), 2.0)
        assert all(moment.tzinfo is None for moment in got[:2])

    def test_error_message_explains_and_names_moments(self):
        with pytest.raises(launch.RunConfigurationError) as exc:
            launch.validate_range(datetime(2023, 6, 1, 12), datetime(2023, 6, 1, 10), 1.0)
        message = str(exc.value)
        assert "раньше конца" in message
        assert "2023-06-01 12:00" in message
        assert "не запущен" in message
