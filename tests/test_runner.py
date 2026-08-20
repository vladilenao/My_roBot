from unittest.mock import patch, MagicMock, call
import pandas as pd
import pytest
from src.scheduler.runner import run_bot


def _make_candle_df():
    return pd.DataFrame({
        'datetime': pd.date_range('2024-01-01', periods=20, freq='1h'),
        'open': [100.0] * 20,
        'high': [101.0] * 20,
        'low': [99.0] * 20,
        'close': [100.5] * 20,
        'volume': [1000] * 20,
    })


def _make_ta_df():
    df = _make_candle_df()
    df['macd_signal'] = 1
    df['rsi_signal'] = 1
    df['stoch_signal'] = 1
    return df


class TestRunBot:
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.make_decision", return_value="决策文本")
    @patch("src.scheduler.runner.get_last_signals", return_value=(1, 1, 1))
    @patch("src.scheduler.runner.tech_analyze")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    def test_single_instrument(self, mock_sleep, mock_load, mock_ta, mock_signals, mock_decision, mock_send):
        ta_df = _make_ta_df()
        mock_ta.return_value = ta_df
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("SBER", "share")])

        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["ticker"] == "SBER"
        assert call_kwargs["instrument_type"] == "share"
        assert call_kwargs["timeframe"] == "1h"
        assert call_kwargs["start_date"] is None
        assert call_kwargs["end_date"] is None
        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[0][0]
        assert "[SBER share]" in sent_msg

    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.make_decision", return_value="决策文本")
    @patch("src.scheduler.runner.get_last_signals", return_value=(1, 1, 1))
    @patch("src.scheduler.runner.tech_analyze")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    def test_multiple_instruments(self, mock_sleep, mock_load, mock_ta, mock_signals, mock_decision, mock_send):
        ta_df = _make_ta_df()
        mock_ta.return_value = ta_df
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("SBER", "share"), ("NGU6", "future")])

        assert mock_load.call_count == 2
        assert mock_send.call_count == 2
        first_msg = mock_send.call_args_list[0][0][0]
        second_msg = mock_send.call_args_list[1][0][0]
        assert "[SBER share]" in first_msg
        assert "[NGU6 future]" in second_msg

    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.make_decision", return_value="决策文本")
    @patch("src.scheduler.runner.get_last_signals", return_value=(1, 1, 1))
    @patch("src.scheduler.runner.tech_analyze")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    def test_empty_df_skips_instrument(self, mock_sleep, mock_load, mock_ta, mock_signals, mock_decision, mock_send):
        mock_load.side_effect = [
            (pd.DataFrame(), "uid-123"),
            (_make_candle_df(), "uid-456"),
        ]
        ta_df = _make_ta_df()
        mock_ta.return_value = ta_df

        run_bot(instruments=[("BAD", "share"), ("SBER", "share")])

        mock_ta.assert_called_once()
        mock_decision.assert_called_once()
        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[0][0]
        assert "[SBER share]" in sent_msg

    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.make_decision")
    @patch("src.scheduler.runner.get_last_signals", return_value=(1, 1, 1))
    @patch("src.scheduler.runner.tech_analyze")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    def test_instrument_label_passed_to_make_decision(self, mock_sleep, mock_load, mock_ta, mock_signals, mock_decision, mock_send):
        ta_df = _make_ta_df()
        mock_ta.return_value = ta_df
        mock_load.return_value = (_make_candle_df(), "uid-123")
        mock_decision.return_value = "decision"

        run_bot(instruments=[("GAZP", "share")])

        mock_decision.assert_called_once_with(1, 1, 1, 100.5, "GAZP share")

    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.make_decision", return_value="决策文本")
    @patch("src.scheduler.runner.get_last_signals", return_value=(1, 1, 1))
    @patch("src.scheduler.runner.tech_analyze")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    def test_message_format_includes_instrument(self, mock_sleep, mock_load, mock_ta, mock_signals, mock_decision, mock_send):
        ta_df = _make_ta_df()
        mock_ta.return_value = ta_df
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("NGU6", "future")])

        sent_msg = mock_send.call_args[0][0]
        assert sent_msg.startswith("[NGU6 future]")
        assert "Сигналы:" in sent_msg

    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.make_decision", return_value="决策文本")
    @patch("src.scheduler.runner.get_last_signals", return_value=(1, 1, 1))
    @patch("src.scheduler.runner.tech_analyze")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    def test_default_instruments_from_config(self, mock_sleep, mock_load, mock_ta, mock_signals, mock_decision, mock_send):
        ta_df = _make_ta_df()
        mock_ta.return_value = ta_df
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot()

        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["ticker"] == "NGU6"
        assert call_kwargs["instrument_type"] == "future"

    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.make_decision", return_value="决策文本")
    @patch("src.scheduler.runner.get_last_signals", return_value=(1, 1, 1))
    @patch("src.scheduler.runner.tech_analyze")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.time.sleep")
    def test_exception_continues_loop(self, mock_sleep, mock_load, mock_ta, mock_signals, mock_decision, mock_send):
        call_count = [0]

        def sleep_side_effect(secs):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        mock_load.side_effect = [Exception("API error"), (_make_candle_df(), "uid-123")]
        ta_df = _make_ta_df()
        mock_ta.return_value = ta_df

        run_bot(instruments=[("SBER", "share")])

        assert mock_send.call_count == 1
