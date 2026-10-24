import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.bot.launch import (
    ALL,
    MODE_EMULATION,
    SOURCE_API,
    Chosen,
    LaunchError,
    LaunchRequest,
    classify_tokens,
    describe_choice,
    equivalent_command,
    parse_instrument_tokens,
    parse_launch_args,
    resolve_emulation,
    resolve_history,
)

SHARES = {"SBER": ["a"], "GAZP": []}
FUTURES = {"NG": ["a"], "RTS": ["b"], "BR": []}
NOW = datetime(2026, 10, 22, 12, 0)


class TestParseLaunchArgs:
    def test_no_arguments_start_robot(self):
        assert parse_launch_args([]) is None

    @pytest.mark.parametrize("argv", [
        ["--mode", "emulation"], ["--mode", "history", "--from", "2026-09-01 10:00", "--to", "2026-09-08 10:00"],
        ["--no-prompt"], ["--wait-command"], ["--instruments", "NG"], ["--pause", "0"],
    ])
    def test_launch_parameters_are_refused_with_api_hint(self, argv):
        with pytest.raises(LaunchError) as exc:
            parse_launch_args(argv)
        message = str(exc.value)
        assert "без параметров" in message and '"kind": "emulation"' in message and '"kind": "history"' in message

    def test_empty_list_rejected(self):
        with pytest.raises(LaunchError):
            parse_instrument_tokens(" , ")

    def test_tokens_are_upper_cased_and_deduplicated(self):
        assert parse_instrument_tokens(["ng", " sber", "NG"]) == ("NG", "SBER")


class TestClassifyTokens:
    def test_all_takes_instruments_with_assignments(self):
        targets, rejections = classify_tokens((ALL,), SHARES, FUTURES)
        assert [(t.kind, t.value) for t in targets] == [("share", "SBER"), ("family", "NG"), ("family", "RTS")]
        assert rejections == []

    def test_family_prefix_and_contract_forms(self):
        targets, rejections = classify_tokens(("RI", "NG-12.26", "NGZ6", "SBER"), SHARES, FUTURES)
        assert [(t.kind, t.family) for t in targets] == [("family", "RTS"), ("contract", "NG"), ("contract", "NG"), ("share", None)]
        assert rejections == []

    def test_without_assignments_rejected(self):
        _, rejections = classify_tokens(("GAZP", "BR", "AFLT"), SHARES, FUTURES)
        reasons = {item.token: item.reason for item in rejections}
        assert "нет привязок" in reasons["GAZP"]
        assert "нет привязок" in reasons["BR"]
        assert "Неизвестный инструмент" in reasons["AFLT"]

    def test_family_and_its_contract_conflict(self):
        targets, rejections = classify_tokens(("NG", "NG-12.26"), SHARES, FUTURES)
        assert [t.token for t in targets] == ["NG"]
        assert rejections[0].token == "NG-12.26" and "в одном запуске" in rejections[0].reason


def _futures(*items):
    return lambda client, market_now: [
        (f"NG (Природный газ) — {name}", ticker, "future", name, NOW + timedelta(days=days)) for ticker, name, days in items
    ]


def _validate(client, ticker, instrument_type, market_now=None):
    if ticker == "BADZ6":
        raise ValueError("не найден")
    if ticker == "BOOM":
        raise ConnectionError("секретный адрес")
    return f"uid-{ticker}", None


class TestResolveEmulation:
    def _resolve(self, tokens, futures, block_days=2):
        return resolve_emulation(tokens, object(), share_strategies=SHARES, future_strategies=FUTURES,
                                 block_days=block_days, market_now=NOW, validate=_validate, active_futures=futures)

    def test_nearest_contract_skips_expiring_one(self):
        result = self._resolve(("NG",), _futures(("NGX6", "NG-11.26", 1), ("NGZ6", "NG-12.26", 31)))
        assert result.ok
        assert result.instruments == [("NG (Природный газ) — NG-12.26", "NGZ6", "future", "NG-12.26")]
        assert describe_choice(result.chosen[0]) == "NG → NG-12.26, до экспирации 31 сутки"

    def test_explicit_contract_by_ticker_is_shown_by_short_name(self):
        result = self._resolve(("NGX6",), _futures(("NGX6", "NG-11.26", 1)))
        assert result.chosen[0].token == "NG-11.26" and result.chosen[0].name == "NG-11.26"

    def test_no_suitable_contract(self):
        result = self._resolve(("NG",), _futures(("NGX6", "NG-11.26", 1)))
        assert not result.ok and "позже чем через 2 суток" in result.rejections[0].reason

    def test_unknown_contract(self):
        result = self._resolve(("NG-03.27",), _futures(("NGZ6", "NG-12.26", 31)))
        assert "не найден" in result.rejections[0].reason

    def test_share_is_validated(self):
        result = self._resolve(("SBER",), _futures())
        assert result.instruments == [("SBER", "SBER", "share", "SBER")]

    def test_broker_errors_become_rejections_without_details(self):
        result = self._resolve(("NG",), _futures(("BADZ6", "NG-12.26", 31)))
        assert "не подтвердил" in result.rejections[0].reason
        result = self._resolve(("NG",), _futures(("BOOM", "NG-12.26", 31)))
        assert result.rejections[0].reason == "Проверка у брокера не выполнена (ConnectionError)."


class TestResolveHistory:
    def test_family_becomes_chain_and_contract_fixed(self):
        client = SimpleNamespace(futures_catalog=lambda: [SimpleNamespace(family="NG", standard=True)])
        result = resolve_history(("NG", "SBER"), client, share_strategies=SHARES, future_strategies=FUTURES,
                                 market_now=NOW, validate=_validate,
                                 active_futures=lambda c, m: [])
        assert result.ok
        assert result.instruments.families == ("NG",)
        assert [item.ticker for item in result.instruments.fixed] == ["SBER"]

    def test_family_missing_in_catalog(self):
        client = SimpleNamespace(futures_catalog=lambda: [])
        result = resolve_history(("NG",), client, share_strategies=SHARES, future_strategies=FUTURES,
                                 market_now=NOW, validate=_validate)
        assert "каталоге эмулятора" in result.rejections[0].reason


class TestCommandAndMenu:
    def test_emulation_command_is_api_body_with_short_names(self):
        request = LaunchRequest(MODE_EMULATION, SOURCE_API, ("NG", "SBER"))
        chosen = [Chosen("NG", "NG-12.26", "future", 52, "NG", True), Chosen("SBER", "SBER", "share")]
        assert json.loads(equivalent_command(request, chosen)) == {"kind": "emulation",
                                                                   "instruments": ["NG-12.26", "SBER"]}

    def test_days_word(self):
        assert describe_choice(Chosen("NG", "NG-12.26", "future", 52, "NG", True)).endswith("52 суток")
        assert describe_choice(Chosen("NG", "NG-12.26", "future", 11, "NG", True)).endswith("11 суток")
