from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import pytest
from src.instruments.selector import (
    select_instruments, _ask_choice, _validate_instruments,
    _select_from_list, _deduplicate, fetch_active_futures,
    _format_futures_display,
    RTS_STOCK_TICKERS, FUTURES_BASES, FUTURES_TTL,
)


def _mock_client():
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _make_future(ticker, name, days_to_expiry=90):
    f = MagicMock()
    f.ticker = ticker
    f.name = name
    f.expiration_date = datetime.now(timezone.utc) + timedelta(days=days_to_expiry)
    f.uid = f"uid-{ticker}"
    return f


def _make_futures_response(futures_list):
    resp = MagicMock()
    resp.instruments = futures_list
    return resp


class TestFormatFuturesDisplay:
    def test_format_with_label(self):
        assert _format_futures_display("NG-9.26", "NG", "Природный газ") == "NG (Природный газ) — NG-9.26"

    def test_format_rts(self):
        assert _format_futures_display("RTS-9.26", "RTS", "Индекс РТС") == "RTS (Индекс РТС) — RTS-9.26"

    def test_format_br(self):
        assert _format_futures_display("BR-12.26", "BR", "Нефть Brent") == "BR (Нефть Brent) — BR-12.26"


class TestDeduplicate:
    def test_no_duplicates(self):
        instruments = [("SBER", "SBER", "share"), ("GAZP", "GAZP", "share")]
        assert _deduplicate(instruments) == [("SBER", "SBER", "share"), ("GAZP", "GAZP", "share")]

    def test_removes_duplicates(self):
        instruments = [("SBER", "SBER", "share"), ("SBER", "SBER", "share"), ("GAZP", "GAZP", "share")]
        result = _deduplicate(instruments)
        assert len(result) == 2
        assert result[0][1] == "SBER"
        assert result[1][1] == "GAZP"

    def test_preserves_first_occurrence(self):
        instruments = [("SBER", "SBER", "share"), ("SBER old", "SBER", "future")]
        result = _deduplicate(instruments)
        assert len(result) == 1
        assert result[0][0] == "SBER"

    def test_empty_list(self):
        assert _deduplicate([]) == []

    def test_single_item(self):
        assert _deduplicate([("SBER", "SBER", "share")]) == [("SBER", "SBER", "share")]


class TestFetchActiveFutures:
    def test_returns_all_futures_within_ttl(self):
        client = _mock_client()
        futures = [
            _make_future("RIU6", "RTS-9.26 Индекс РТС", 30),
            _make_future("RIZ6", "RTS-12.26 Индекс РТС", 120),
            _make_future("RIH7", "RTS-3.27 Индекс РТС", 180),
        ]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        tickers = [t for _, t, _ in result]
        assert "RIU6" in tickers
        assert "RIZ6" in tickers
        assert "RIH7" in tickers

    def test_grouped_by_base_then_sorted_by_expiry(self):
        client = _mock_client()
        futures = [
            _make_future("RIZ6", "RTS-12.26 Индекс РТС", 120),
            _make_future("RIU6", "RTS-9.26 Индекс РТС", 30),
            _make_future("BRZ6", "BR-12.26 Нефть Brent", 120),
            _make_future("BRU6", "BR-9.26 Нефть Brent", 30),
        ]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        tickers = [t for _, t, _ in result]
        assert tickers == ["RIU6", "RIZ6", "BRU6", "BRZ6"]

    def test_excludes_micro_mini(self):
        client = _mock_client()
        futures = [
            _make_future("SIU6", "Si-9.26 Курс Доллар – Рубль", 90),
            _make_future("RMU6", "RTSM-9.26 Индекс РТС (мини)", 90),
        ]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        tickers = [t for _, t, _ in result]
        assert "SIU6" in tickers
        assert "RMU6" not in tickers

    def test_excludes_expired(self):
        client = _mock_client()
        futures = [_make_future("RIU6", "RTS-9.26 Индекс РТС", -10)]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        assert len(result) == 0

    def test_excludes_far_expiry(self):
        client = _mock_client()
        futures = [_make_future("RIU6", "RTS-9.26 Индекс РТС", 400)]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        assert len(result) == 0

    def test_all_bases_present(self):
        client = _mock_client()
        futures = [
            _make_future("RIU6", "RTS-9.26 Индекс РТС", 30),
            _make_future("BRU6", "BR-9.26 Нефть Brent", 30),
            _make_future("SIU6", "Si-9.26 Курс Доллар – Рубль", 30),
            _make_future("SRU6", "SBRF-9.26 Сбер Банк", 30),
            _make_future("EDU6", "ED-9.26 Курс Евро - Доллар", 30),
            _make_future("NGU6", "NG-9.26 Природный газ", 30),
            _make_future("GDU6", "GOLD-9.26 Золото", 30),
            _make_future("SVU6", "SILV-9.26 Серебро", 30),
        ]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        assert len(result) == 8
        assert all(item[2] == "future" for item in result)

    def test_empty_futures_list(self):
        client = _mock_client()
        client.instruments.futures.return_value = _make_futures_response([])
        result = fetch_active_futures(client)
        assert result == []

    def test_display_shows_full_format(self):
        client = _mock_client()
        futures = [_make_future("NGU6", "NG-9.26 Природный газ", 30)]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        assert len(result) == 1
        display = result[0][0]
        assert display == "NG (Природный газ) — NG-9.26"

    def test_display_rts_format(self):
        client = _mock_client()
        futures = [_make_future("RIU6", "RTS-9.26 Индекс РТС", 30)]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        assert result[0][0] == "RTS (Индекс РТС) — RTS-9.26"

    def test_display_si_format(self):
        client = _mock_client()
        futures = [_make_future("SIU6", "Si-9.26 Курс Доллар – Рубль", 30)]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        assert result[0][0] == "Si (Доллар – Рубль) — Si-9.26"

    def test_multiple_bases_multiple_contracts(self):
        client = _mock_client()
        futures = [
            _make_future("RIU6", "RTS-9.26 Индекс РТС", 30),
            _make_future("RIZ6", "RTS-12.26 Индекс РТС", 120),
            _make_future("BRU6", "BR-9.26 Нефть Brent", 30),
            _make_future("BRZ6", "BR-12.26 Нефть Brent", 120),
        ]
        client.instruments.futures.return_value = _make_futures_response(futures)
        result = fetch_active_futures(client)
        displays = [d for d, _, _ in result]
        assert displays == [
            "RTS (Индекс РТС) — RTS-9.26",
            "RTS (Индекс РТС) — RTS-12.26",
            "BR (Нефть Brent) — BR-9.26",
            "BR (Нефть Brent) — BR-12.26",
        ]


class TestAskChoice:
    @patch("builtins.input", return_value="1")
    def test_valid_choice(self, mock_input):
        assert _ask_choice("Выбор: ", ("1", "2")) == "1"

    @patch("builtins.input", side_effect=["x", "2"])
    def test_invalid_then_valid(self, mock_input):
        assert _ask_choice("Выбор: ", ("1", "2")) == "2"
        assert mock_input.call_count == 2

    @patch("builtins.input", return_value="да")
    def test_yes_variants(self, mock_input):
        assert _ask_choice("Ещё? ", ("да", "нет")) == "да"


class TestValidateInstruments:
    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    def test_all_valid_stocks(self, mock_find):
        client = _mock_client()
        entries = [("SBER", "SBER", "share"), ("GAZP", "GAZP", "share")]
        result = _validate_instruments(client, entries, "share")
        assert len(result) == 2
        assert result[0] == ("SBER", "SBER", "share")
        assert result[1] == ("GAZP", "GAZP", "share")

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    def test_all_valid_futures(self, mock_find):
        client = _mock_client()
        entries = [("NG (Природный газ) — NG-9.26", "NGU6", "future")]
        result = _validate_instruments(client, entries, "future")
        assert len(result) == 1
        assert result[0] == ("NG (Природный газ) — NG-9.26", "NGU6", "future")

    @patch("src.instruments.selector.find_working_instrument")
    def test_some_invalid(self, mock_find):
        mock_find.side_effect = ["uid-123", ValueError("не найден")]
        client = _mock_client()
        entries = [("SBER", "SBER", "share"), ("BAD", "BAD", "share")]
        result = _validate_instruments(client, entries, "share")
        assert len(result) == 1
        assert result[0][1] == "SBER"

    @patch("src.instruments.selector.find_working_instrument")
    def test_all_invalid(self, mock_find):
        mock_find.side_effect = ValueError("не найден")
        client = _mock_client()
        entries = [("BAD1", "BAD1", "share"), ("BAD2", "BAD2", "share")]
        result = _validate_instruments(client, entries, "share")
        assert result == []


class TestSelectFromList:
    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", return_value="1,3")
    def test_select_by_numbers(self, mock_input, mock_find):
        client = _mock_client()
        entries = [("SBER", "SBER", "share"), ("GAZP", "GAZP", "share"), ("LKOH", "LKOH", "share")]
        result = _select_from_list(client, entries, "share")
        assert len(result) == 2
        assert result[0][1] == "SBER"
        assert result[1][1] == "LKOH"

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", return_value="SBER,LKOH")
    def test_select_by_tickers(self, mock_input, mock_find):
        client = _mock_client()
        entries = [("SBER", "SBER", "share"), ("GAZP", "GAZP", "share"), ("LKOH", "LKOH", "share")]
        result = _select_from_list(client, entries, "share")
        assert len(result) == 2

    @patch("builtins.input", return_value="")
    def test_empty_input_returns_empty(self, mock_input):
        client = _mock_client()
        result = _select_from_list(client, [("SBER", "SBER", "share")], "share")
        assert result == []

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", return_value="99")
    def test_out_of_range_number(self, mock_input, mock_find):
        client = _mock_client()
        result = _select_from_list(client, [("SBER", "SBER", "share")], "share")
        assert result == []

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", return_value="1")
    def test_futures_entry(self, mock_input, mock_find):
        client = _mock_client()
        entries = [("NG (Природный газ) — NG-9.26", "NGU6", "future")]
        result = _select_from_list(client, entries, "future")
        assert len(result) == 1
        assert result[0] == ("NG (Природный газ) — NG-9.26", "NGU6", "future")

    @patch("builtins.input", return_value="")
    def test_empty_entries_returns_empty(self, mock_input):
        client = _mock_client()
        result = _select_from_list(client, [], "future")
        assert result == []


class TestSelectInstruments:
    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["1", "1", "", "нет"])
    def test_stocks_only(self, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert len(result) == 1
        assert result[0] == ("SBER", "SBER", "share")

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["2", "1", "", "нет"])
    @patch("src.instruments.selector.fetch_active_futures", return_value=[
        ("NG (Природный газ) — NG-9.26", "NGU6", "future"),
    ])
    def test_futures_only(self, mock_fetch, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert result == [("NG (Природный газ) — NG-9.26", "NGU6", "future")]

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["1", "1,2", "", "да", "2", "1", "", "нет"])
    @patch("src.instruments.selector.fetch_active_futures", return_value=[
        ("NG (Природный газ) — NG-9.26", "NGU6", "future"),
    ])
    def test_stocks_then_futures(self, mock_fetch, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        types = [item[2] for item in result]
        assert "share" in types
        assert "future" in types

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["1", "", "нет", "1", "1", "", "нет"])
    def test_empty_then_valid_triggers_retry(self, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert len(result) >= 1

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["1", "1", "", "нет"])
    def test_client_used_as_context_manager(self, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        ctx.__enter__.assert_called_once()
        ctx.__exit__.assert_called_once()

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["1", "1,3", "", "нет"])
    def test_multiple_stocks_selected(self, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert len(result) == 2
        assert all(item[2] == "share" for item in result)

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["x", "1", "1", "", "нет"])
    def test_invalid_type_choice_retry(self, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert result == [("SBER", "SBER", "share")]

    @patch("src.instruments.selector.find_working_instrument")
    @patch("builtins.input", side_effect=["1", "1", "", "нет", "1", "1", "", "нет"])
    def test_invalid_instruments_then_valid(self, mock_input, mock_find):
        mock_find.side_effect = [ValueError("не найден"), "uid-123"]
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert len(result) >= 1

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["2", "нет", "1", "SBER", "", "нет"])
    @patch("src.instruments.selector.fetch_active_futures", return_value=[])
    def test_empty_futures_shows_message(self, mock_fetch, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert len(result) >= 1
        mock_fetch.assert_called_once_with(ctx)

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["2", "нет", "1", "SBER", "", "нет"])
    @patch("src.instruments.selector.fetch_active_futures", side_effect=Exception("API error"))
    def test_futures_api_error_continues(self, mock_fetch, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert len(result) >= 1

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["1", "1,1", "", "нет"])
    def test_same_ticker_twice_deduplicated(self, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert result == [("SBER", "SBER", "share")]

    @patch("src.instruments.selector.find_working_instrument", return_value="uid-123")
    @patch("builtins.input", side_effect=["1", "1", "", "да", "1", "1", "", "нет"])
    def test_same_stock_across_rounds_deduplicated(self, mock_input, mock_find):
        ctx = _mock_client()
        with patch("src.instruments.selector.client_context", return_value=ctx):
            result = select_instruments()
        assert result == [("SBER", "SBER", "share")]


class TestConstants:
    def test_rts_stock_count(self):
        assert len(RTS_STOCK_TICKERS) == 15

    def test_futures_bases_count(self):
        assert len(FUTURES_BASES) == 8

    def test_futures_bases_contain_expected_names(self):
        names = [b["name"] for b in FUTURES_BASES]
        for n in ["RTS", "BR", "Si", "SBRF", "ED", "NG", "GOLD", "SILV"]:
            assert n in names

    def test_rts_stocks_are_uppercase(self):
        for t in RTS_STOCK_TICKERS:
            assert t == t.upper()

    def test_ttl_is_6_months(self):
        assert FUTURES_TTL.days == 183
