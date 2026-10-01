from datetime import datetime
from unittest.mock import patch

import pytest
import run


class TestAskMode:
    @patch("builtins.input", return_value="1")
    def test_one_selects_live_without_more_questions(self, _input):
        assert run.ask_mode() == run.MODE_LIVE
        assert _input.call_count == 1

    @patch("builtins.input", side_effect=["2", "2023-06-01 10:00", "2023-06-01 12:00", "0.5"])
    def test_two_starts_history(self, _input):
        assert run.ask_mode() == run.MODE_HISTORY
        assert _input.call_count == 1

    @patch("builtins.input", return_value="1")
    def test_no_prompt_skips_dialog(self, _input):
        assert run.ask_mode(no_prompt=True) == run.MODE_LIVE
        _input.assert_not_called()

    @patch("builtins.input", side_effect=["", "3", " 1 "])
    def test_invalid_answers_are_repeated(self, _input, capsys):
        assert run.ask_mode() == run.MODE_LIVE
        assert _input.call_count == 3
        assert capsys.readouterr().out.count("Нужен ответ 1 или 2.") == 2

    @patch("builtins.input", side_effect=["боевая", "2"])
    def test_words_are_rejected(self, _input):
        assert run.ask_mode() == run.MODE_HISTORY


class TestParseMoment:
    def test_naive_input_is_utc(self):
        assert run.parse_moment("2023-06-01 10:00") == datetime(2023, 6, 1, 10, 0)

    def test_explicit_zone_converted_to_utc(self):
        assert run.parse_moment("2023-06-01T13:00:00+03:00") == datetime(2023, 6, 1, 10, 0)

    def test_z_suffix_parsed(self):
        assert run.parse_moment("2023-06-01T10:00:00Z") == datetime(2023, 6, 1, 10, 0)

    def test_date_only_means_midnight(self):
        assert run.parse_moment("2023-06-01") == datetime(2023, 6, 1, 0, 0)

    def test_surrounding_spaces_ignored(self):
        assert run.parse_moment("  2023-06-01 10:00 ") == datetime(2023, 6, 1, 10, 0)

    @pytest.mark.parametrize("raw", ["", "   ", "01.06.2023", "завтра", "2023-13-01 10:00"])
    def test_unparsable_input_raises(self, raw):
        with pytest.raises(ValueError):
            run.parse_moment(raw)

    def test_error_message_shows_expected_format(self):
        with pytest.raises(ValueError) as exc:
            run.parse_moment("01.06.2023")
        assert "ГГГГ-ММ-ДД ЧЧ:ММ" in str(exc.value)


class TestValidateRange:
    def test_valid_range_passes_through(self):
        start, end = datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 12)
        assert run.validate_range(start, end, 0.0) == (start, end, 0.0)

    def test_equal_moments_rejected(self):
        moment = datetime(2023, 6, 1, 10)
        with pytest.raises(run.RunConfigurationError):
            run.validate_range(moment, moment, 1.0)

    def test_reversed_range_rejected(self):
        with pytest.raises(run.RunConfigurationError):
            run.validate_range(datetime(2023, 6, 1, 12), datetime(2023, 6, 1, 10), 1.0)

    def test_timezone_aware_input_normalized_to_naive(self):
        from datetime import timezone

        start = datetime(2023, 6, 1, 13, tzinfo=timezone.utc)
        end = datetime(2023, 6, 1, 16, tzinfo=timezone.utc)
        got = run.validate_range(start, end, 2)

        assert got == (datetime(2023, 6, 1, 13), datetime(2023, 6, 1, 16), 2.0)
        assert all(moment.tzinfo is None for moment in got[:2])

    def test_error_message_explains_and_names_moments(self):
        with pytest.raises(run.RunConfigurationError) as exc:
            run.validate_range(datetime(2023, 6, 1, 12), datetime(2023, 6, 1, 10), 1.0)
        message = str(exc.value)
        assert "раньше конца" in message
        assert "2023-06-01 12:00" in message
        assert "не запущен" in message


class TestAskPause:
    @patch("builtins.input", side_effect=["2.5"])
    def test_numeric_pause_parsed(self, _input):
        assert run._ask_pause() == 2.5

    @patch("builtins.input", return_value="")
    def test_default_pause_on_empty_input(self, _input):
        assert run._ask_pause() == run.HISTORY_DEFAULT_PAUSE == 1.0

    @patch("builtins.input", side_effect=["быстро", "-1", "0.25"])
    def test_bad_pause_asks_again(self, _input, capsys):
        assert run._ask_pause() == 0.25
        out = capsys.readouterr().out
        assert "числом секунд" in out
        assert "не может быть отрицательной" in out

    @patch("builtins.input", side_effect=["0"])
    def test_zero_pause_allowed(self, _input):
        assert run._ask_pause() == 0.0


class TestAskHistoryRange:
    @patch("builtins.input", side_effect=["2023-06-01 10:00", "2023-06-01 12:00", ""])
    def test_full_dialog_returns_range_and_default_pause(self, _input):
        assert run.ask_history_range() == (
            datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 12), 1.0
        )

    @patch("builtins.input", side_effect=["позавчера", "2023-06-01 10:00", "2023-06-01 12:00", "0"])
    def test_bad_date_asks_again(self, _input, capsys):
        start, end, pause = run.ask_history_range()

        assert start == datetime(2023, 6, 1, 10)
        assert end == datetime(2023, 6, 1, 12)
        assert pause == 0.0
        assert "ГГГГ-ММ-ДД ЧЧ:ММ" in capsys.readouterr().out

    @patch("builtins.input", side_effect=["2023-06-01 12:00", "2023-06-01 10:00", "1"])
    def test_wrong_boundaries_raise_before_run(self, _input):
        with pytest.raises(run.RunConfigurationError):
            run.ask_history_range()


class TestMainRouting:
    @patch("run._run_history", return_value=0)
    @patch("run._run_live", return_value=0)
    @patch("builtins.input", side_effect=["2", "2023-06-01 10:00", "2023-06-01 12:00", "1"])
    def test_history_mode_runs_history(self, _input, run_live, run_history):
        assert run.main() == 0
        run_history.assert_called_once()
        run_live.assert_not_called()

    @patch("run._run_history", return_value=0)
    @patch("run._run_live", return_value=0)
    @patch("builtins.input", return_value="1")
    def test_live_mode_runs_live(self, _input, run_live, run_history):
        assert run.main() == 0
        run_live.assert_called_once()
        run_history.assert_not_called()

    @patch("run._run_history", return_value=0)
    @patch("run._run_live", return_value=0)
    def test_no_prompt_flag_runs_live(self, run_live, run_history):
        assert run.main(no_prompt=True) == 0
        run_live.assert_called_once()
        run_history.assert_not_called()

    @patch("run._run_history", return_value=0)
    @patch("run._run_live", return_value=0)
    @patch("builtins.input", side_effect=["2", "2023-06-01 12:00", "2023-06-01 10:00", "1"])
    def test_wrong_boundaries_stop_without_starting(self, _input, run_live, run_history, capsys):
        assert run.main() == 1
        run_live.assert_not_called()
        run_history.assert_not_called()
        assert "раньше конца" in capsys.readouterr().out


class TestDescribeScale:
    def test_ticks_and_estimate_shown(self):
        text = run.describe_scale(
            datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 12), 0.5, ["1m"]
        )

        assert "Обработано тиков: 120" in text
        assert "паузе 0.5 с" in text
        assert "1 мин" in text
        assert "HIST" in text

    def test_hour_long_range_reported_in_hours(self):
        text = run.describe_scale(
            datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 14), 1.0, ["1m"]
        )

        assert "Обработано тиков: 240" in text
        assert "4 мин" in text

    def test_step_comes_from_smallest_active_timeframe(self):
        text = run.describe_scale(
            datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 11), 1.0, ["5m", "1m", "1h"]
        )

        assert "Шаг тика:        0:01:00" in text
        assert "Обработано тиков: 60" in text


    def test_zero_pause_reported_as_no_waiting(self):
        text = run.describe_scale(
            datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 12), 0.0, ["1m"]
        )

        assert "Обработано тиков: 120" in text
        assert "Ожидание между тиками: нет" in text
        assert "длительность определяется обработкой" in text
        assert "паузе 0 с" not in text


class TestStateDirFor:
    def test_history_state_dir_is_under_hist(self):
        state = run.state_dir_for(datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 12))

        assert state.parent == run.runtime_dir(run.Path(run.HIST_DIR))
        assert state.name.endswith("-20230601T100000-20230601T120000")

    def test_live_and_history_state_dirs_differ(self):
        history = run.state_dir_for(datetime(2023, 6, 1, 10), datetime(2023, 6, 1, 12))
        assert history != run.runtime_dir()
