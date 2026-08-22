from unittest.mock import patch, MagicMock
import pandas as pd
import pytest

from src.scheduler.runner import run_bot
from src.strategies.base import Decision, SignalType


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


def _make_strategy(name="mock_strat", decision=None):
    strategy = MagicMock()
    strategy.NAME = name
    strategy.compute.return_value = _make_ta_df()
    strategy.decide.return_value = decision or Decision(SignalType.BUY, 100.5)
    return strategy


SHARE_STRATEGIES = {
    "SBER": ["macd_rsi_stoch"],
    "GAZP": ["macd_rsi_stoch"],
    "BAD": ["macd_rsi_stoch"],
}

FUTURE_STRATEGIES = {
    "NG": ["macd_rsi_stoch"],
    "MU": ["macd_rsi_stoch", "macd_rsi_stoch"],
}


class TestRunBot:
    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_single_instrument_three_tuple(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("SBER", "SBER", "share")])

        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["ticker"] == "SBER"
        assert call_kwargs["instrument_type"] == "share"
        mock_get.assert_called_once_with("macd_rsi_stoch")
        strategy.compute.assert_called_once()
        strategy.decide.assert_called_once_with(strategy.compute.return_value)
        mock_send.assert_called_once()

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_single_instrument_two_tuple_label_fallback(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("GAZP", "share")])

        sent_msg = mock_send.call_args[0][0]
        assert sent_msg.startswith("[GAZP share]")
        assert "ПОКУПАТЬ" in sent_msg

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_futures_use_display_name(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("NG (Природный газ) — NG-9.26", "NGU6", "future")])

        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["ticker"] == "NGU6"
        assert call_kwargs["instrument_type"] == "future"
        sent_msg = mock_send.call_args[0][0]
        assert sent_msg.startswith("[NG (Природный газ) — NG-9.26]")

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_multiple_instruments_processed_in_order(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        instruments = [
            ("SBER", "SBER", "share"),
            ("NG (Природный газ) — NG-9.26", "NGU6", "future"),
        ]
        run_bot(instruments=instruments)

        assert mock_load.call_count == 2
        assert mock_send.call_count == 2
        labels = [call[0][0].split("]")[0] for call in mock_send.call_args_list]
        assert labels == ["[SBER", "[NG (Природный газ) — NG-9.26"]

    @patch("src.scheduler.runner.SHARE_STRATEGIES", {"SBER": ["macd_rsi_stoch"]})
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", {"NG": ["macd_rsi_stoch"]})
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_future_binding_by_base_ignores_contract_suffix(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("NG (Природный газ) — NG-12.26", "NGZ7", "future")])

        mock_get.assert_called_once_with("macd_rsi_stoch")
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["ticker"] == "NGZ7"

    @patch("src.scheduler.runner.SHARE_STRATEGIES", {})
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", {"SI": ["macd_rsi_stoch"]})
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_future_base_lookup_is_case_insensitive(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("Si (Доллар – Рубль) — SiZ6", "SiZ6", "future")])

        mock_get.assert_called_once_with("macd_rsi_stoch")
        mock_load.assert_called_once()

    @patch("src.scheduler.runner.SHARE_STRATEGIES", {"SBER": ["macd_rsi_stoch"]})
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", {})
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_share_ticker_not_looked_up_in_futures_dict(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("SBER", "SBER", "share")])

        mock_get.assert_called_once_with("macd_rsi_stoch")
        mock_load.assert_called_once()
        mock_send.assert_called_once()

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_empty_df_skips_instrument(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.side_effect = [
            (pd.DataFrame(), "uid-bad"),
            (_make_candle_df(), "uid-ok"),
        ]

        run_bot(instruments=[("BAD", "BAD", "share"), ("SBER", "SBER", "share")])

        strategy.compute.assert_called_once()
        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[0][0]
        assert "[SBER]" in sent_msg

    @patch("src.scheduler.runner.SHARE_STRATEGIES", {})
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", {})
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_unassigned_ticker_skipped_without_load(self, mock_get, mock_load, mock_send, mock_sleep, capsys):
        run_bot(instruments=[("XXX", "XXX", "share"), ("SBER", "SBER", "share")])

        mock_load.assert_not_called()
        mock_get.assert_not_called()
        mock_send.assert_not_called()
        assert "не назначено стратегий" in capsys.readouterr().out

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_strategy_failure_does_not_block_next(self, mock_get, mock_load, mock_send, mock_sleep):
        failing = _make_strategy()
        failing.compute.side_effect = Exception("boom")
        working = _make_strategy()
        mock_get.side_effect = [failing, working]
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("MULTI", "MULTI", "future")])

        assert working.compute.called is True
        mock_send.assert_called_once()

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_message_format_no_diagnostics_line(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy(decision=Decision(SignalType.BUY, 100.4567))
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("NGU6", "NGU6", "future")])

        sent_msg = mock_send.call_args[0][0]
        assert sent_msg == "[NGU6] 🚀 ПОКУПАТЬ! Цена: 100.457"
        assert "Сигналы:" not in sent_msg

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_hold_message(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy(decision=Decision(SignalType.HOLD, 100.5))
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot(instruments=[("SBER", "SBER", "share")])

        sent_msg = mock_send.call_args[0][0]
        assert "😴 Отдыхаем, сигналов нет." in sent_msg
        assert "Цена" not in sent_msg

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep", side_effect=KeyboardInterrupt)
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_default_instruments_from_config(self, mock_get, mock_load, mock_send, mock_sleep):
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.return_value = (_make_candle_df(), "uid-123")

        run_bot()

        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["ticker"] == "NGU6"
        assert call_kwargs["instrument_type"] == "future"
        mock_get.assert_called_once_with("macd_rsi_stoch")

    @patch("src.scheduler.runner.SHARE_STRATEGIES", SHARE_STRATEGIES)
    @patch("src.scheduler.runner.FUTURE_STRATEGIES", FUTURE_STRATEGIES)
    @patch("src.scheduler.runner.time.sleep")
    @patch("src.scheduler.runner.send_signal")
    @patch("src.scheduler.runner.load_candles")
    @patch("src.scheduler.runner.get_strategy")
    def test_exception_continues_loop(self, mock_get, mock_load, mock_send, mock_sleep):
        calls = [0]

        def sleep_side_effect(secs):
            calls[0] += 1
            if calls[0] >= 2:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        strategy = _make_strategy()
        mock_get.return_value = strategy
        mock_load.side_effect = [Exception("API error"), (_make_candle_df(), "uid-123")]

        run_bot(instruments=[("SBER", "SBER", "share")])

        assert mock_send.call_count == 1

    @pytest.mark.parametrize(
        "invalid_share, invalid_future, instrument",
        [
            ({"SBER": ["no_such_strategy"]}, {}, ("SBER", "SBER", "share")),
            ({}, {"NG": ["no_such_strategy"]}, ("NGU6", "future")),
        ],
    )
    @patch("src.scheduler.runner.load_candles")
    def test_fail_fast_on_unknown_strategy_name(self, mock_load, invalid_share, invalid_future, instrument):
        with patch("src.scheduler.runner.SHARE_STRATEGIES", invalid_share), \
             patch("src.scheduler.runner.FUTURE_STRATEGIES", invalid_future):
            with pytest.raises(ValueError) as exc_info:
                run_bot(instruments=[instrument])

        message = str(exc_info.value)
        assert "no_such_strategy" in message
        assert "macd_rsi_stoch" in message
        mock_load.assert_not_called()
