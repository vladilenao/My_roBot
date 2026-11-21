"""Связки прогона истории (add-history-preflight-api)."""

from src.bot.launch import classify_tokens
from src.history.bindings import selection_bindings, target_bindings
from src.history.selection import HistorySelection
from src.instruments.model import normalize_instrument
from src.strategies.contracts import Assignment

NG_MACD = Assignment("future-ng-macd", "macd", "levels_rr", "triple_screen", timeframe="1h")
NG_MA = Assignment("future-ng-ma", "ma_cloud_rsi_macd", "ma_cloud", "raw", timeframe="15m")
SBER = Assignment("share-sber-macd", "macd", "levels_rr", timeframe="4h")
SHARES = {"SBER": [SBER]}
FUTURES = {"NG": [NG_MACD, NG_MA]}


def _row(binding, instrument):
    return {"id": binding.id, "instrument": instrument, "strategy": str(binding.strategy),
            "timeframe": binding.timeframe, "filter": binding.filter_profile, "management": binding.management}


def test_targets_give_bindings_of_share_family_and_contract():
    targets, rejections = classify_tokens(("SBER", "NG"), SHARES, FUTURES)
    assert rejections == []
    assert target_bindings(targets, SHARES, FUTURES) == [
        _row(SBER, "SBER"), _row(NG_MACD, "NG"), _row(NG_MA, "NG")]
    targets, _ = classify_tokens(("NG-7.26",), SHARES, FUTURES)
    assert [row["instrument"] for row in target_bindings(targets, SHARES, FUTURES)] == ["NG-7.26", "NG-7.26"]


def test_all_gives_every_binding():
    targets, _ = classify_tokens(("ALL",), SHARES, FUTURES)
    assert [row["id"] for row in target_bindings(targets, SHARES, FUTURES)] == [
        "share-sber-macd", "future-ng-macd", "future-ng-ma"]


def test_selection_bindings_name_contract_not_exchange_ticker():
    selection = HistorySelection(
        fixed=(normalize_instrument(("SBER", "SBER", "share", "SBER")),
               normalize_instrument(("NGN6", "NGN6", "future", "NG-7.26")),
               normalize_instrument(("NGQ6", "NGQ6", "future"))),
        families=(),
    )
    rows = selection_bindings(selection, SHARES, FUTURES)
    assert [row["instrument"] for row in rows] == ["SBER", "NG-7.26", "NG-7.26", "контракт не указан",
                                                   "контракт не указан"]
    assert all("NGN6" not in str(row) for row in rows)


def test_selection_bindings_of_chain_family():
    assert selection_bindings(HistorySelection(families=("NG",)), SHARES, FUTURES) == [
        _row(NG_MACD, "NG"), _row(NG_MA, "NG")]


def test_plain_instrument_list():
    assert selection_bindings([normalize_instrument(("SBER", "share"))], SHARES, FUTURES) == [_row(SBER, "SBER")]


def test_contour_report_bindings_follow_run_selection():
    from types import SimpleNamespace

    from src.assembly.contour import TradingContour

    settings = SimpleNamespace(strategies=(SHARES, FUTURES), instruments=["ignored"],
                               session=SimpleNamespace(contract_selection=HistorySelection(families=("NG",))))
    assert [row["id"] for row in TradingContour._bindings(SimpleNamespace(settings=settings))] == [
        "future-ng-macd", "future-ng-ma"]
    broken = SimpleNamespace(strategies=(SHARES, FUTURES), instruments=[object()], session=SimpleNamespace())
    assert TradingContour._bindings(SimpleNamespace(settings=broken)) is None
