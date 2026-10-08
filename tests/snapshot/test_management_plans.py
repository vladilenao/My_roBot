"""Снимки планов управления на реальных барах.

План — это числа трейдера: где стоит стоп, куда смотрят цели, почему стоп именно
здесь и сколько это стоит в деньгах. Ни одна из этих величин не проверяется
построчно в unit-тестах: там важна логика правила, а не то, как оно ложится на
конкретный рынок. Снимок отвечает на другое — на реальном корпусе NG 15m видно,
что общее правило геометрии действительно применяется к живому рынку: узкий
структурный стоп поднимается до пола, далёкий уровень срезается потолком, а между
ними ни одна из границ не рушит план.

Корпус настоящий (``NGV6``, 15 минут) и содержит оба края на одних и те же
настройках из ``default.toml``: узкие предложения дают последние бары с
``min_stop_ticks``-полом, широкие — далёкие уровни S/R и паттерна.

Перезаписать эталоны:

    .venv/bin/python -m pytest tests/snapshot/test_management_plans.py --update-snapshots
"""

import math
import json
from decimal import Decimal

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from src.config_loader import app_dir, load_config
from src.market_context.models import MarketContext
from src.market_context.sr_levels import SRLevelsCalculator
from src.market_context.trend import TrendAnalyzer
from src.strategies.contracts import Decision, SignalType
from src.trade_management.economics import plan_economics
from src.trade_management.pipeline import PROFILE_CLASSES, build_management_market, build_profile_snapshot
from src.trade_management.profiles.base import PlanningContext, ProfileResult
from src.trade_management.profiles.geometry import StopPolicy
from tests.snapshot import helper

WARMUP = 60
STRIDE = 16
FIGURE_BARS = 8
COMMISSION = 1.5
SLIPPAGE = 1.0
QUANTITY = 3
PRICE_STEP = 0.001
STEP_COST = 8.4
_RTOL = 1e-9
# План живёт на сетке тиков, а ATR на разных платформах (Windows x64 vs macOS
# arm64, numba/LLVM) расходится в последних ульпах: floor(-стопа) может
# перекинуть границу тика. Снимок обязан терпеть ровно один тик (0.0015
# принимает 1 тик, но отвергает 2 — realных значений, лежащих на сетке).
_ATOL = 1.5 * PRICE_STEP

_COLUMNS = [
    "datetime",
    "side",
    "reason",
    "entry",
    "proposed_stop",
    "stop",
    "targets",
    "stop_basis",
    "atr",
    "risk_amount",
    "reward_amount",
    "costs_amount",
    "payoff_ratio",
    "quantity", "budget_risk_amount", "fixed_reward_amount", "fixed_quantity",
    "slippage_amount", "target_quantities", "algorithm_version",
]
# ``targets`` — тоже текст: пустая строка в CSV читается как NaN, и без этого
# сравнение ловит разницу записи, а не разницу плана.
_TEXT = ["side", "reason", "stop_basis", "targets", "target_quantities", "algorithm_version"]

# Профили, у которых ширину стопа диктует уровень рынка, а не собственное правило.
# Только на них корпус обязан показать оба края: ``atr_trend`` задаёт стоп
# кратным ATR, а ``pattern_targets`` — точкой C, их узкий диапазон — свойство
# профиля, а не бедность корпуса.
LEVEL_DRIVEN = ("levels_rr", "ma_cloud")


def _profiles_config() -> dict:
    """Профили из поставляемого ``default.toml``, а не из тестового заполнителя."""
    bundled = app_dir() / "default.toml"
    config = load_config({}, config_file=bundled.parent / "no_such_robot.toml", bundled_file=bundled)
    return config["trade_management_profiles"]


class _Contract:
    price_step = PRICE_STEP
    step_cost = STEP_COST


def _context(frame: pd.DataFrame) -> MarketContext:
    price = float(frame["close"].iloc[-1])
    return MarketContext(
        trend=TrendAnalyzer().analyze(frame),
        sr_levels=list(SRLevelsCalculator().compute(frame, price)),
        current_price=price,
    )


def _pattern_signal(frame: pd.DataFrame) -> tuple[SignalType, dict[str, object]]:
    """Фигура последних баров: c — край последнего бара, d — противоположный край.

    Направление выбирается так, чтобы точка d была впереди цены входа: иначе
    профилю нечего планировать, и строка снимка была бы пустым отказом вместо
    проверки геометрии.
    """
    close = float(frame["close"].iloc[-1])
    window = frame.iloc[-FIGURE_BARS:]
    high = float(window["high"].max())
    low = float(window["low"].min())
    last_low = float(frame["low"].iloc[-1])
    last_high = float(frame["high"].iloc[-1])
    bar_time = frame["datetime"].iloc[-1]
    if high > close:
        return SignalType.BUY, {
            "pattern_id": f"NG-15m-buy-{bar_time}",
            "c": last_low,
            "d": high,
            "time_available": bar_time,
        }
    if low < close:
        return SignalType.SELL, {
            "pattern_id": f"NG-15m-sell-{bar_time}",
            "c": last_high,
            "d": low,
            "time_available": bar_time,
        }
    return SignalType.HOLD, {}


def _plan_row(profile_name: str, frame: pd.DataFrame) -> dict:
    parameters = _profiles_config()[profile_name]
    snapshot = build_profile_snapshot(profile_name, parameters)
    market = build_management_market(
        contract=_Contract(),
        frame=frame,
        context=_context(frame),
        profile_parameters=dict(snapshot.parameters),
        price=float(frame["close"].iloc[-1]),
        signal=False,
        commission=COMMISSION,
        slippage=SLIPPAGE,
    )
    signal_type, references = _pattern_signal(frame)
    bar_time = frame["datetime"].iloc[-1]
    decision = Decision(
        signal_type=signal_type,
        price=float(frame["close"].iloc[-1]),
        bar_time=bar_time,
        event_id=f"NG-15m-{bar_time}",
        available_at=bar_time,
        idea_references=references,
    )
    profile = PROFILE_CLASSES[profile_name]()
    context = PlanningContext(
        trade_id=decision.event_id,
        assignment_id=f"{profile_name}-snapshot",
        instrument_id="NGV6",
        signal=decision,
        profile=snapshot,
        market=market,
    )
    plan, _trace = profile.plan_with_trace(context)
    # ``plan`` объявлен через ``shared_planning_rules``, поэтому предложение
    # профиля видно только в необработанном методе: без него снимок не может
    # показать, каким стопом профиль хотел пожертвовать полом или потолком.
    proposed = _proposed_stop(profile, context)
    row = {
        "datetime": bar_time,
        "side": "",
        "reason": "",
        "entry": None,
        "proposed_stop": None,
        "stop": None,
        "targets": "",
        "stop_basis": "",
        "atr": None if market["atr"] is None else float(market["atr"]),
        "risk_amount": None,
        "reward_amount": None,
        "costs_amount": None,
        "payoff_ratio": None,
        "quantity": None, "budget_risk_amount": None, "fixed_reward_amount": None,
        "fixed_quantity": None, "slippage_amount": None, "target_quantities": "",
        "algorithm_version": str(market.get("algorithm_version", "legacy-v1")),
    }
    if isinstance(plan, ProfileResult) or plan is None:
        state = plan.state if isinstance(plan, ProfileResult) else {}
        row["reason"] = str(state.get("reason", "profile-rejected"))
        return row
    if proposed is not None:
        row["proposed_stop"] = float(proposed)
    economics = plan_economics(
        plan,
        quantity=QUANTITY,
        price_step=market["price_step"],
        step_cost=market["step_cost"],
        market=market,
    )
    row.update(
        side=plan.side,
        entry=float(plan.reference_entry),
        stop=float(plan.stop_price),
        targets="|".join(str(float(target.price)) for target in plan.targets),
        stop_basis=plan.stop_basis.value,
        risk_amount=float(economics.risk_amount),
        reward_amount=None if economics.reward_amount is None else float(economics.reward_amount),
        costs_amount=float(economics.costs_amount),
        payoff_ratio=None if economics.payoff_ratio is None else float(economics.payoff_ratio),
        quantity=economics.quantity,
        budget_risk_amount=float(economics.budget_risk_amount),
        fixed_reward_amount=None if economics.fixed_reward_amount is None else float(economics.fixed_reward_amount),
        fixed_quantity=economics.fixed_quantity,
        slippage_amount=float(economics.slippage_amount),
        target_quantities=json.dumps(dict(economics.target_quantities), sort_keys=True),
        algorithm_version=plan.algorithm_version,
    )
    return row


def _proposed_stop(profile, context: PlanningContext) -> float | None:
    """Стоп, который предложил сам профиль, до общей геометрии."""
    unbound = getattr(type(profile).plan, "__wrapped__", None)
    if unbound is None:
        return None
    result = unbound(profile, context)
    return None if isinstance(result, ProfileResult) else float(result.stop_price)


def _expected_plans(case: str, profile_name: str) -> pd.DataFrame:
    df = helper.load_candles_fixture(case)
    indices = range(WARMUP, len(df), STRIDE)
    return pd.DataFrame(
        [_plan_row(profile_name, df.iloc[: index + 1]) for index in indices],
        columns=_COLUMNS,
    )


def _discover_cases() -> list[tuple[str, str]]:
    if not helper.DATA_DIR.exists():
        return []
    cases = set()
    for expected_path in helper.DATA_DIR.glob("*/*_expected_plans.csv"):
        cases.add((expected_path.parent.name, expected_path.name[: -len("_expected_plans.csv")]))
    return sorted(cases)


CASES = _discover_cases()
pytestmark = pytest.mark.skipif(not CASES, reason="нет корпусов с эталонными планами")


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    """Привести прогон и эталон к одному виду: даты, текст, числа.

    Иначе сравнение ловит не план, а запись: пустая строка в CSV читается как
    NaN, а колонка, где все значения None, получает при чтении другой dtype.
    """
    frame = frame.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.as_unit("ns")
    for column in _TEXT:
        frame[column] = frame[column].astype("string").fillna("")
    for column in frame.columns.difference(["datetime"] + _TEXT):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame[_COLUMNS]


@pytest.mark.parametrize("case,profile_name", CASES)
def test_management_plan_snapshot(case, profile_name, request):
    actual = _expected_plans(case, profile_name)
    path = helper.DATA_DIR / case / f"{profile_name}_expected_plans.csv"

    if request.config.getoption("--update-snapshots"):
        _normalize(actual).to_csv(path, index=False)
        return

    expected = _normalize(pd.read_csv(path))
    try:
        assert_frame_equal(_normalize(actual), expected, check_exact=False, rtol=_RTOL, atol=_ATOL)
    except AssertionError as error:
        position, got, want = _first_divergence(actual, expected)
        pytest.fail(
            f"план {profile_name} на баре {position} разошёлся: "
            f"получено {got}, ожидалось {want}\n{error}"
        )


def _first_divergence(actual: pd.DataFrame, expected: pd.DataFrame) -> tuple[int, dict | None, dict | None]:
    """Первый разошедшийся бар плана: так правило ловится глазами, а не через assert_frame_equal."""
    text = [column for column in _TEXT if column in actual.columns]
    numbers = [column for column in actual.columns if column not in text + ["datetime"]]
    for position in range(max(len(actual), len(expected))):
        if position >= len(actual):
            return position, None, expected.iloc[position].to_dict()
        if position >= len(expected):
            return position, actual.iloc[position].to_dict(), None
        got, want = actual.iloc[position], expected.iloc[position]
        differs = (
            got["datetime"] != want["datetime"]
            or any(got[column] != want[column] for column in text)
            or any(
                not math.isclose(
                    float(got[column]),
                    float(want[column]),
                    rel_tol=_RTOL,
                    abs_tol=_ATOL,
                )
                if pd.notna(got[column]) and pd.notna(want[column])
                else pd.isna(got[column]) != pd.isna(want[column])
                for column in numbers
            )
        )
        if differs:
            return position, got.to_dict(), want.to_dict()
    return -1, None, None


def _planned_rows(case: str, profile_name: str) -> pd.DataFrame:
    frame = _normalize(pd.read_csv(helper.DATA_DIR / case / f"{profile_name}_expected_plans.csv"))
    return frame[frame["entry"].notna() & frame["atr"].notna() & (frame["atr"] > 0)]


@pytest.mark.parametrize("case,profile_name", [case for case in CASES if case[1] in LEVEL_DRIVEN])
def test_corpus_reproduces_both_stop_widths(case, profile_name):
    """Корпус обязан воспроизводить оба края геометрии в единицах ATR.

    Без узкого предложения (~0.25×ATR) нечего проверять пол, без широкого (>3×ATR)
    нечего проверять потолок: снимок, где стоп всегда ровно 1.5×ATR, охранял бы
    одну строчку формулы и молчал бы о второй.
    """
    planned = _planned_rows(case, profile_name)
    if planned.empty:
        pytest.skip(f"{profile_name} не спланировал ни одного входа на корпусе {case}")
    atr = planned["atr"].astype(float)
    proposed = (planned["entry"].astype(float) - planned["proposed_stop"].astype(float)).abs()
    assert (proposed / atr).min() <= 0.5, (
        f"{profile_name} на {case}: минимальное предложение "
        f"{(proposed / atr).min():.2f}×ATR — узкий край не воспроизведён"
    )
    assert (proposed / atr).max() > 3.0, (
        f"{profile_name} на {case}: максимальное предложение "
        f"{(proposed / atr).max():.2f}×ATR — широкий край не воспроизведён"
    )


@pytest.mark.parametrize("case", sorted({name for name, _profile in CASES}))
def test_corpus_as_a_whole_covers_both_edges(case):
    """Свойство корпуса, а не профиля: где-то в NG_15m стоп обязан быть и узким, и широким."""
    narrow = wide = False
    for profile_name in sorted(profile for case_name, profile in CASES if case_name == case):
        planned = _planned_rows(case, profile_name)
        if planned.empty:
            continue
        widths = (planned["entry"].astype(float) - planned["proposed_stop"].astype(float)).abs() / planned["atr"].astype(float)
        narrow = narrow or widths.min() <= 0.5
        wide = wide or widths.max() > 3.0
    assert narrow, f"корпус {case}: нет узких стопов (~0.25×ATR) — пол нечего проверять"
    assert wide, f"корпус {case}: нет широких стопов (>3×ATR) — потолок нечего проверять"


@pytest.mark.parametrize("case,profile_name", CASES)
def test_recorded_stops_respect_the_configured_bounds(case, profile_name):
    """Записанный стоп обязан лежать между полом и потолком из default.toml.

    Проверка идёт от эталонного файла, а не от результата прогона, поэтому
    ловит и расхождение формулы, и расхождение самого эталона.
    """
    planned = _planned_rows(case, profile_name)
    if planned.empty:
        pytest.skip(f"{profile_name} не спланировал ни одного входа на корпусе {case}")
    policy = StopPolicy.from_parameters(_profiles_config()[profile_name])
    atr = [float(value) for value in planned["atr"]]
    tick = PRICE_STEP
    for entry, stop, basis, value in zip(
        planned["entry"], planned["stop"], planned["stop_basis"], atr
    ):
        distance = abs(float(entry) - float(stop))
        floor = float(policy.floor(atr=_decimal(value), bar_range=None, price_step=_decimal(tick)))
        cap = policy.cap(_decimal(value))
        assert distance + 1e-9 >= floor, f"{basis}: стоп {distance} уже пола {floor}"
        assert distance >= tick - 1e-9, f"{basis}: стоп {distance} уже одного тика {tick}"
        if cap is not None:
            assert distance <= float(cap) + tick + 1e-9, f"{basis}: стоп {distance} выше потолка {float(cap)}"


@pytest.mark.parametrize("case,profile_name", CASES)
def test_cap_is_an_exception_not_the_norm(case, profile_name):
    """Потолок, срабатывающий на каждом втором плане, — это не страховка, а замена геометрии."""
    planned = _planned_rows(case, profile_name)
    if planned.empty:
        pytest.skip(f"{profile_name} не спланировал ни одного входа на корпусе {case}")
    capped = (planned["stop_basis"] == "atr-cap").mean()
    assert capped < 0.1, f"{profile_name} на {case}: потолок сработал в {capped:.0%} планов"


def _decimal(value) -> Decimal:
    return Decimal(str(value))
