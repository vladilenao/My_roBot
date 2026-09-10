"""
Генерация SVG-картинок сигналов стратегий для спецификаций OpenSpec.

Для каждой стратегии и snapshot-кейса строит рисунок событий BUY/SELL:
ценовой график (классические бары OHLC, опционально полосы Боллинджера
поверх цены), панели индикаторов под ним, жирный серый пунктир на свече
входа и тонкий — на границе окна анализа, стрелку «BUY/SELL ВХОД» и
горизонтальный текст справа от каждой панели с человекочитаемым описанием
сигналов за окно стратегии.

Результат сохраняется в openspec/assets/signals/<стратегия>_<BUY|SELL>.svg
и подключается в спецификации по root-relative пути /openspec/assets/signals/...

Пример:
    python tools/visualize_signals.py --strategy macd_rsi_stoch --case BR_1h
    python tools/visualize_signals.py --strategy flat_triangle --case BR_1h
    python tools/visualize_signals.py --strategy macd_rsi_stoch --case BR_1h --direction SELL
"""

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.strategies import get_strategy
from src.strategies.base_strategy import StrategyConfig
from src.strategies.macd_rsi_stoch_strategy import DEFAULT_CONFIG as MACD_RSI_STOCH_CONFIG
from src.strategies.flat_triangle_strategy import DEFAULT_CONFIG as FLAT_TRIANGLE_CONFIG
from src.strategies.harmonic_abcd_strategy import DEFAULT_CONFIG as HARMONIC_ABCD_CONFIG
from src.market_structure.harmonic import HarmonicPatternDetector, Direction
from src.market_structure.fibonacci import retracement_level
from src.market_structure.swings import SwingDetector, SwingKind
from src.strategies.indicators.macd.signalEnum import MacdSignalEnum
from src.strategies.indicators.rsi.signalEnum import RsiSignalEnum
from src.strategies.indicators.stochastic.signalEnum import StochasticSignalEnum

STRATEGY_CONFIGS = {
    "macd_rsi_stoch": MACD_RSI_STOCH_CONFIG,
    "flat_triangle": FLAT_TRIANGLE_CONFIG,
    "harmonic_abcd": HARMONIC_ABCD_CONFIG,
}

DATA_DIR = PROJECT_ROOT / "tests" / "snapshot" / "data"
ASSETS_DIR = PROJECT_ROOT / "openspec" / "assets" / "signals"

LOOKBACK = 40    # свечей до входа на рисунке
FORWARD = 8      # свечей после входа на рисунке

# ── человекочитаемые тексты сигналов индикаторов ─────────────────

SIGNAL_TEXT = {
    "macd_signal": {
        int(MacdSignalEnum.BULLISH_CROSSOVER_BELOW_ZERO): "бычий кроссовер (сигнальная пересекла MACD вверх) ниже нуля",
        int(MacdSignalEnum.BEARISH_CROSSOVER_ABOVE_ZERO): "медвежий кроссовер (сигнальная пересекла MACD вниз) выше нуля",
    },
    "rsi_signal": {
        int(RsiSignalEnum.CROSS_ABOVE_50): "пересечение 50 снизу вверх",
        int(RsiSignalEnum.CROSS_BELOW_50): "пересечение 50 сверху вниз",
    },
    "stoch_signal": {
        int(StochasticSignalEnum.EXIT_OVERSOLD): "выход %K из перепроданности (<20 → >20)",
        int(StochasticSignalEnum.EXIT_OVERBOUGHT): "выход %K из перекупленности (>80 → <80)",
    },
}

# ── имена колонок индикаторов из их параметров ───────────────────


def _macd_columns(indicator):
    s = indicator.slow
    f = indicator.fast
    si = indicator.signal
    return f"macd_{f}_{s}_{si}", f"macds_{f}_{s}_{si}", f"macdh_{f}_{s}_{si}"


def _stoch_columns(indicator):
    k, d, sm = indicator.k, indicator.d, indicator.smooth_k
    return f"stochk_{k}_{d}_{sm}", f"stochd_{k}_{d}_{sm}"


def _bb_columns(indicator):
    return (
        f"bbl_{indicator.length}_{indicator.std}",
        f"bbm_{indicator.length}_{indicator.std}",
        f"bbu_{indicator.length}_{indicator.std}",
    )


# ── рисование панелей ────────────────────────────────────────────


def _draw_bars_classic(ax, sub, bands=None):
    for i, (o, h, l, c) in enumerate(zip(sub["open"], sub["high"], sub["low"], sub["close"])):
        color = "#26a69a" if c >= o else "#ef5350"
        ax.plot([i, i], [l, h], color=color, lw=1.0, zorder=3)
        ax.plot([i - 0.3, i], [o, o], color=color, lw=1.4, zorder=4)
        ax.plot([i, i + 0.3], [c, c], color=color, lw=1.4, zorder=4)
    if bands:
        bbl, bbm, bbu = bands
        x = range(len(sub))
        ax.plot(x, sub[bbl], color="#7cb342", lw=1.0, ls="--", zorder=2, label="BB low")
        ax.plot(x, sub[bbm], color="#78909c", lw=0.9, ls="-.", zorder=2, label="BB mid")
        ax.plot(x, sub[bbu], color="#e53935", lw=1.0, ls="--", zorder=2, label="BB up")
        ax.legend(loc="upper left", fontsize=7, ncol=3, frameon=False)


def _draw_macd(ax, sub, cols):
    macd_col, signal_col, hist_col = cols
    x = range(len(sub))
    ax.axhline(0, color="#bbbbbb", lw=0.8)
    ax.plot(x, sub[macd_col], color="#1e88e5", lw=1.2, label="MACD")
    ax.plot(x, sub[signal_col], color="#fb8c00", lw=1.2, label="Signal")
    ax.bar(x, sub[hist_col], color="#90caf9", width=0.7, label="Hist")
    ax.set_ylabel("MACD", rotation=0, labelpad=25)
    ax.legend(loc="upper left", fontsize=7, ncol=3, frameon=False)


def _draw_rsi(ax, sub, col):
    x = range(len(sub))
    ax.axhspan(70, 100, color="#ffcdd2", alpha=0.35, zorder=0)
    ax.axhspan(0, 30, color="#c5e1a5", alpha=0.35, zorder=0)
    ax.axhline(50, color="#bbbbbb", lw=0.8)
    ax.axhline(30, color="#7cb342", lw=0.8, ls="--")
    ax.axhline(70, color="#e53935", lw=0.8, ls="--")
    ax.text(0.99, 0.965, "перекупленность >70", transform=ax.transAxes, ha="right", va="top", fontsize=6.5, color="#b71c1c")
    ax.text(0.99, 0.02, "перепроданность <30", transform=ax.transAxes, ha="right", va="bottom", fontsize=6.5, color="#33691e")
    ax.plot(x, sub[col], color="#7b1fa2", lw=1.2)
    ax.set_ylabel("RSI", rotation=0, labelpad=25)
    ax.set_yticks([0, 30, 50, 70, 100])


def _draw_stoch(ax, sub, cols):
    k_col, d_col = cols
    x = range(len(sub))
    ax.axhspan(80, 100, color="#ffcdd2", alpha=0.35, zorder=0)
    ax.axhspan(0, 20, color="#c5e1a5", alpha=0.35, zorder=0)
    ax.axhline(20, color="#7cb342", lw=0.8, ls="--")
    ax.axhline(80, color="#e53935", lw=0.8, ls="--")
    ax.axhline(50, color="#bbbbbb", lw=0.8)
    ax.text(0.99, 0.965, "перекупленность >80", transform=ax.transAxes, ha="right", va="top", fontsize=6.5, color="#b71c1c")
    ax.text(0.99, 0.02, "перепроданность <20", transform=ax.transAxes, ha="right", va="bottom", fontsize=6.5, color="#33691e")
    ax.plot(x, sub[k_col], color="#00897b", lw=1.2, label="%K")
    ax.plot(x, sub[d_col], color="#e53935", lw=1.2, label="%D")
    ax.set_ylabel("Stoch", rotation=0, labelpad=25)
    ax.legend(loc="upper left", fontsize=7, ncol=2, frameon=False)
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 20, 50, 80, 100])


def _mark_stoch_entry(ax, sub, cols, ei):
    """Точки %K/%D и значения у свечи входа, заметные даже у кромки панели."""
    k_col, d_col = cols
    k, d = float(sub[k_col].iloc[ei]), float(sub[d_col].iloc[ei])
    lo, hi = ax.get_ylim()
    for val, color, name in ((k, "#00897b", "%K"), (d, "#e53935", "%D")):
        ax.scatter([ei], [val], s=42, color=color, edgecolors="white", linewidths=0.7,
                   zorder=7)
        near_top = val > hi - 0.05 * (hi - lo)
        near_bottom = val < lo + 0.05 * (hi - lo)
        va = "bottom" if near_top else ("top" if near_bottom else "bottom")
        ax.text(ei + 0.35, val, f"{name} {val:.1f}", fontsize=7.5, color=color, va=va, zorder=7)


# ── описания панелей ─────────────────────────────────────────────


def _desc_events_by_signal(kind, sig_col, window, sub, entry_i, value_line):
    """События индикатора за окно стратегии + текущее значение."""
    w = sub.iloc[entry_i - window + 1 : entry_i + 1]
    events = []
    if sig_col in SIGNAL_TEXT:
        unique_events = {int(e) for e in w[sig_col].unique() if e != 0}
        events = [SIGNAL_TEXT[sig_col][e] for e in sorted(unique_events)]
    text_lines = [f"{kind}: {ev}" for ev in events] if events else [f"{kind}: без сигналов за окно"]
    text_lines.append(value_line(sub.iloc[entry_i]))
    return "\n".join(text_lines)


# ── конфиг панелей на стратегию ──────────────────────────────────


def _build_panel_config_macd(cfg):
    macd, rsi, stoch = cfg.indicators
    macd_cols = _macd_columns(macd)
    stoch_cols = _stoch_columns(stoch)
    value_lines = {
        "macd": lambda r: f"MACD {r[macd_cols[0]]:.2f} / Signal {r[macd_cols[1]]:.2f}",
        "rsi": lambda r: f"RSI {r['rsi']:.1f}",
        "stoch": lambda r: f"%K {r[stoch_cols[0]]:.1f} / %D {r[stoch_cols[1]]:.1f}",
    }
    return {
        "window": cfg.strategy_window,
        "panels": [
            {
                "kind": "price",
                "draw": lambda ax, sub: _draw_bars_classic(ax, sub),
                "describe": lambda sub, ei, direction: _desc_events_by_signal(
                    "Цена", None, cfg.strategy_window, sub, ei,
                    lambda r: f"close {r['close']:.2f}"
                ),
            },
            {
                "kind": "macd", "label": "MACD", "signal_col": "macd_signal",
                "draw": lambda ax, sub: _draw_macd(ax, sub, macd_cols),
                "describe": lambda sub, ei, direction: _desc_events_by_signal(
                    "MACD", "macd_signal", cfg.strategy_window, sub, ei, value_lines["macd"]
                ),
            },
            {
                "kind": "rsi", "label": "RSI", "signal_col": "rsi_signal",
                "draw": lambda ax, sub: _draw_rsi(ax, sub, "rsi"),
                "describe": lambda sub, ei, direction: _desc_events_by_signal(
                    "RSI", "rsi_signal", cfg.strategy_window, sub, ei, value_lines["rsi"]
                ),
            },
            {
                "kind": "stoch", "label": "Stoch", "signal_col": "stoch_signal",
                "stoch_cols": stoch_cols,
                "draw": lambda ax, sub: _draw_stoch(ax, sub, stoch_cols),
                "describe": lambda sub, ei, direction: _desc_events_by_signal(
                    "Stoch", "stoch_signal", cfg.strategy_window, sub, ei, value_lines["stoch"]
                ),
            },
        ],
    }


def _build_panel_config_flat(cfg):
    bb, rsi, stoch = cfg.indicators
    bbl, bbm, bbu = _bb_columns(bb)
    stoch_cols = _stoch_columns(stoch)
    window = cfg.strategy_window

    def describe_price(sub, ei, direction):
        row = sub.iloc[ei]
        close, lower, mid, upper = row["close"], row[bbl], row[bbm], row[bbu]
        if direction == "SELL":
            relation = (
                f"Цена {close:.2f} — находится за верхней полосой\n"
                f"BB {upper:.2f}: перегрев, рынок перекуплен, возможен откат"
            )
        else:
            relation = (
                f"Цена {close:.2f} — находится за нижней полосой\n"
                f"BB {lower:.2f}: перепроданность, возможен отскок"
            )
        return "\n".join([relation, f"Полосы BB: {lower:.2f} … {mid:.2f} … {upper:.2f}"])

    def describe_rsi(sub, ei, direction):
        row = sub.iloc[ei]
        v = row["rsi"]
        zone = "находится в зоне перепроданности (<30)" if v < 30 else (
            "находится в зоне перекупленности (>70)" if v > 70 else "в нейтральной зоне"
        )
        return f"RSI {v:.1f} — {zone}"

    def describe_stoch(sub, ei, direction):
        row = sub.iloc[ei]
        k, d = row[stoch_cols[0]], row[stoch_cols[1]]
        k_prev, d_prev = sub[stoch_cols[0]].iloc[ei - 1], sub[stoch_cols[1]].iloc[ei - 1]
        cross = "пересечение: %K пересекла %D снизу вверх" if k_prev <= d_prev and k > d else \
                "пересечение: %K пересекла %D сверху вниз" if k_prev >= d_prev and k < d else ""
        zone = "находится в зоне перепроданности (<20)" if k < 20 else (
            "находится в зоне перекупленности (>80)" if k > 80 else "в нейтральной зоне"
        )
        return "\n".join([f"%K {k:.1f} {cross}", f"%D {d:.1f} — {zone}"]).strip()

    return {
        "window": window,
        "panels": [
            {
                "kind": "price",
                "draw": lambda ax, sub: _draw_bars_classic(ax, sub, bands=(bbl, bbm, bbu)),
                "describe": describe_price,
            },
            {
                "kind": "rsi", "label": "RSI",
                "draw": lambda ax, sub: _draw_rsi(ax, sub, "rsi"),
                "describe": describe_rsi,
            },
            {
                "kind": "stoch", "label": "Stoch", "stoch_cols": stoch_cols,
                "draw": lambda ax, sub: _draw_stoch(ax, sub, stoch_cols),
                "describe": describe_stoch,
            },
        ],
    }


def _build_panel_config_harmonic(cfg):
    return {
        "window": cfg.strategy_window,
        "harmonic": True,
        "panels": [],
    }


def build_panel_config(strategy_name):
    cfg = STRATEGY_CONFIGS[strategy_name]
    if strategy_name == "macd_rsi_stoch":
        return _build_panel_config_macd(cfg)
    if strategy_name == "flat_triangle":
        return _build_panel_config_flat(cfg)
    if strategy_name == "harmonic_abcd":
        return _build_panel_config_harmonic(cfg)
    raise ValueError(
        f"Для стратегии '{strategy_name}' нет конфигурации панелей. "
        f"Доступны: {sorted(STRATEGY_CONFIGS)}"
    )


PANEL_CONFIG = {
    strategy_name: build_panel_config(strategy_name)
    for strategy_name in STRATEGY_CONFIGS
    if strategy_name in ("macd_rsi_stoch", "flat_triangle", "harmonic_abcd")
}


# ── harmonic_abcd: формация AB=CD ────────────────────────────────


def _find_harmonic_pattern(df, entry_i):
    """Паттерн AB=CD, чей вход совпадает с баром `entry_i`."""
    detector = HarmonicPatternDetector()
    patterns = detector.analyze(df)
    candidates = [
        p for p in patterns if p.c.index + detector.right == entry_i
    ]
    if not candidates:
        return None
    # при конфликте лонг/шорт приоритет у лонга (как в стратегии)
    longs = [p for p in candidates if p.direction is Direction.LONG]
    return longs[0] if longs else candidates[0]


def _draw_harmonic(ax, sub, pattern, offset, swings, d_hit_loc=None):
    _draw_bars_classic(ax, sub)
    col = "#1e88e5" if pattern.direction is Direction.LONG else "#e53935"

    # фибо-зоны волны XA (стиль TradingView: сплошная лёгкая заливка,
    # насыщенность растёт к средним уровням)
    x_price, a_price = pattern.x.price, pattern.a.price
    fib_levels = (0.0, 0.382, 0.5, 0.618, 0.786, 1.0)
    levels = [retracement_level(a_price, x_price, r) for r in fib_levels]
    band_alphas = (0.08, 0.16, 0.22, 0.16, 0.08)
    for (lo_r, hi_r), alpha in zip(zip(fib_levels, fib_levels[1:]), band_alphas):
        lo_y, hi_y = sorted((retracement_level(a_price, x_price, lo_r),
                             retracement_level(a_price, x_price, hi_r)))
        ax.axhspan(lo_y, hi_y, color="#f9a825", alpha=alpha, zorder=2, lw=0)
    for r, lvl in zip(fib_levels[1:-1], levels[1:-1]):
        ax.axhline(lvl, color="#f9a825", lw=0.9, ls=":", alpha=0.95, zorder=3)
        ax.text(0.004, lvl, f"{r:.1%}", transform=ax.get_yaxis_transform(),
                ha="left", va="top", fontsize=6.5, color="#5d4037", zorder=6)
    ax.axhline(levels[0], color="#bdbdbd", lw=0.8, alpha=0.8, zorder=3)
    ax.axhline(levels[-1], color="#bdbdbd", lw=0.8, alpha=0.8, zorder=3)
    ax.text(0.004, levels[0], "0.0%", transform=ax.get_yaxis_transform(),
            ha="left", va="top", fontsize=6.5, color="#5d4037", zorder=6)
    ax.text(0.004, levels[-1], "100%", transform=ax.get_yaxis_transform(),
            ha="left", va="top", fontsize=6.5, color="#5d4037", zorder=6)

    # цель D (161.8%)
    d_target = pattern.d_target
    ax.axhline(d_target, color="#9e9e9e", lw=1.4, ls="--", alpha=0.95, zorder=4)
    ax.text(0.004, d_target, "161.8% цель D", transform=ax.get_yaxis_transform(),
            ha="left", va="bottom", fontsize=7, color="#424242", zorder=6,
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="#9e9e9e", lw=0.7))

    # бар достижения цели D (вход → цель)
    if d_hit_loc is not None and 0 <= d_hit_loc < len(sub):
        reach_price = sub["high"].iloc[d_hit_loc] if pattern.direction is Direction.LONG \
            else sub["low"].iloc[d_hit_loc]
        ax.plot([d_hit_loc], [d_target], marker="o", ms=8, color="#43a047",
                mfc="white", mew=1.8, zorder=9)
        ax.annotate(
            f"Цель D достигнута: {reach_price:.2f}",
            xy=(d_hit_loc, d_target),
            xytext=(d_hit_loc, d_target + (d_target - a_price) * 0.06),
            fontsize=8, color="#2e7d32", fontweight="bold", va="bottom",
            ha="center", zorder=9,
        )

    # свинги в окне (контекст колебаний)
    for s in swings:
        yi = s.index - offset
        if not (0 <= yi < len(sub)):
            continue
        marker = "^" if s.kind is SwingKind.HIGH else "v"
        ax.plot([yi], [s.price], marker=marker, ms=6, color="#424242", mfc="#ffe082",
                mew=0.9, zorder=6)

    # ключевые точки и волны X→A→B→C
    xs = [pt.index - offset for pt in (pattern.x, pattern.a, pattern.b, pattern.c)]
    ys = [pt.price for pt in (pattern.x, pattern.a, pattern.b, pattern.c)]
    if all(0 <= x < len(sub) for x in xs):
        ax.plot(xs, ys, color=col, lw=2.0, ls="-", zorder=7, alpha=0.9)
        ax.annotate("", xy=(xs[1], ys[1]), xytext=(xs[0], ys[0]),
                    arrowprops=dict(arrowstyle="-|>", color=col, lw=2.0,
                                    mutation_scale=14), zorder=8)
        ax.annotate("", xy=(xs[2], ys[2]), xytext=(xs[1], ys[1]),
                    arrowprops=dict(arrowstyle="-|>", color=col, lw=2.0,
                                    mutation_scale=14), zorder=8)
        ax.annotate("", xy=(xs[3], ys[3]), xytext=(xs[2], ys[2]),
                    arrowprops=dict(arrowstyle="-|>", color=col, lw=2.0,
                                    mutation_scale=14), zorder=8)

    for pt, name in ((pattern.x, "X"), (pattern.a, "A"),
                     (pattern.b, "B"), (pattern.c, "C")):
        yi = pt.index - offset
        if 0 <= yi < len(sub):
            ax.plot([yi], [pt.price], marker="o", ms=7, color=col, mfc="white",
                    mew=1.8, zorder=9)
            span = sub["high"].max() - sub["low"].min()
            if pt.kind is SwingKind.HIGH:
                label_y = pt.price + span * 0.006
                va = "bottom"
                ha = "right"
                x_loc = yi - 1.2
                if name == "B":
                    ha = "left"
                    x_loc = yi + 0.3
                    label_y = pt.price + span * 0.008
            else:
                label_y = pt.price - span * 0.006
                va = "top"
                ha = "right"
                x_loc = yi - 1.2
                if name == "C":
                    x_loc = yi + 0.9
                    label_y = pt.price - span * 0.004
            ax.annotate(
                f"{name} {pt.price:.2f}",
                xy=(yi, pt.price),
                xytext=(x_loc, label_y),
                fontsize=8, color=col, fontweight="bold", va=va, ha=ha,
                zorder=9,
                bbox=dict(boxstyle="round,pad=0.18", fc="#ffffff", ec="none",
                          lw=0, alpha=0.9),
            )


def _describe_harmonic(pattern, d_hit_loc=None):
    """Человекочитаемое описание формации AB=CD."""
    x, a, b, c = pattern.x.price, pattern.a.price, pattern.b.price, pattern.c.price
    amp_xa = abs(x - a)
    amp_ab = abs(a - b)
    b_retr = abs(b - a) / amp_xa * 100 if amp_xa else 0
    c_retr = abs(c - b) / amp_ab * 100 if amp_ab else 0
    kind = "бычья (лонг)" if pattern.direction is Direction.LONG else "медвежья (шорт)"
    lines = [
        f"Формация AB=CD ({kind}):",
        f"X {x:.2f} → A {a:.2f} — нисходящая волна" if pattern.direction is Direction.LONG
        else f"X {x:.2f} → A {a:.2f} — восходящая волна",
        f"B {b:.2f} — откат волны XA на {b_retr:.1f}% (38.2–61.8)",
        f"C {c:.2f} — откат волны AB на {c_retr:.1f}% (38.2–78.6)",
        f"Цель D = 161.8% XA → {pattern.d_target:.2f}",
        f"Вход на баре подтверждения после точки C",
    ]
    if d_hit_loc is not None:
        lines.append("Цена дошла до цели D после входа")
    return "\n".join(lines)


def _find_d_target_hit(ta, entry_i, pattern):
    """Первый бар после входа, где цена достигла цели D (high ≥ D для лонга,
    low ≤ D для шорта). None — цель не достигнута в доступной истории."""
    if pattern.direction is Direction.LONG:
        mask = ta["high"].iloc[entry_i:] >= pattern.d_target
    else:
        mask = ta["low"].iloc[entry_i:] <= pattern.d_target
    if not mask.any():
        return None
    return entry_i + int(mask.idxmax())


# ── load ──────────────────────────────────────────────────────────


def _load_candles(case):
    df = pd.read_csv(DATA_DIR / case / "candles.csv")
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.as_unit("ns")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype("float64")
    return df


def _load_expected(case, strategy_name):
    expected = pd.read_csv(DATA_DIR / case / f"{strategy_name}_expected_signals.csv")
    expected["datetime"] = pd.to_datetime(expected["datetime"]).dt.as_unit("ns")
    expected["signal"] = expected["signal"].astype("string")
    return expected


# ── render ────────────────────────────────────────────────────────


def render(strategy_name, case, direction, out):
    config = PANEL_CONFIG.get(strategy_name)
    if config is None:
        raise ValueError(
            f"Для стратегии '{strategy_name}' нет конфигурации панелей. "
            f"Доступны: {sorted(PANEL_CONFIG)}"
        )
    cfg = STRATEGY_CONFIGS[strategy_name]
    strategy = get_strategy(strategy_name, config=cfg)
    df = _load_candles(case)
    ta = strategy.compute(df)

    expected = _load_expected(case, strategy_name)
    events = expected[expected["signal"] == direction]
    if events.empty:
        raise ValueError(
            f"В кейсе '{case}' нет событий '{direction}' для стратегии '{strategy_name}'."
        )
    entry = events.iloc[0]
    entry_date = entry["datetime"]
    entry_i = int((ta["datetime"] == entry_date).idxmax())

    if config.get("harmonic"):
        pattern = _find_harmonic_pattern(df, entry_i)
        if pattern is None:
            raise ValueError(
                f"В кейсе '{case}' для входа {entry_date} не найдена формация AB=CD."
            )
        detector = HarmonicPatternDetector()
        all_swings = SwingDetector(left=detector.left, right=detector.right).detect(df)
        swings = [s for s in all_swings if s.index + detector.right <= entry_i]
        lo = min(pattern.x.index, pattern.a.index, pattern.b.index, pattern.c.index) - 4
        lo = min(lo, entry_i - LOOKBACK)
        if lo < 0:
            lo = 0
        d_hit = _find_d_target_hit(ta, entry_i, pattern)
        hi = max(entry_i, d_hit + FORWARD if d_hit is not None else entry_i + FORWARD,
                 pattern.b.index, pattern.c.index)
        sub = ta.iloc[lo:hi].reset_index(drop=True)
        ei = entry_i - lo
        hi_loc = d_hit - lo if d_hit is not None else None
        panels = [
            {
                "kind": "price",
                "draw": lambda ax, sub: _draw_harmonic(ax, sub, pattern, lo, swings, hi_loc),
                "describe": lambda sub, ei, direction: _describe_harmonic(pattern, hi_loc),
            }
        ]
    else:
        lo, hi = entry_i - LOOKBACK, entry_i + FORWARD
        if lo < 0:
            lo = 0
        sub = ta.iloc[lo:hi].reset_index(drop=True)
        ei = entry_i - lo
        panels = config["panels"][:]
    fig, axs = plt.subplots(
        len(panels), 1, figsize=(15, 2.7 + 1.4 * len(panels)), sharex=True,
        gridspec_kw={"hspace": 0.10},
    )
    axs = list(axs) if len(panels) > 1 else [axs]

    for ax, panel in zip(axs, panels):
        ax.grid(True, color="#f0f0f0", lw=0.6)
        panel["draw"](ax, sub)
        if panel["kind"] == "price":
            ax.set_ylabel("Цена", rotation=0, labelpad=40)

    # пунктиры: жирный на входе, тонкий на границе окна анализа (если окно > 1)
    if config["window"] > 1:
        for ax in axs:
            ax.axvline(ei - config["window"] + 1, color="#c8c8c8", lw=0.7, ls="--", zorder=0)
    for ax in axs:
        ax.axvline(ei, color="#888888", lw=1.6, ls="--", zorder=0)

    axs[0].annotate(
        f"{direction} ВХОД",
        xy=(ei, sub["high"].iloc[ei]),
        xytext=(ei + 6, sub["high"].iloc[ei] + (sub["high"].max() - sub["low"].min()) * 0.12),
        arrowprops=dict(arrowstyle="->", color="#d32f2f", lw=2.4, shrinkA=0, shrinkB=4),
        ha="left", color="#d32f2f", fontsize=10, fontweight="bold",
    )

    for ax, panel in zip(axs, panels):
        text = panel["describe"](sub, ei, direction)
        if panel["kind"] == "stoch":
            _mark_stoch_entry(ax, sub, panel["stoch_cols"], ei)
        ax.text(
            1.015, 0.5, text, transform=ax.transAxes, rotation=0,
            va="center", ha="left", fontsize=8.5, color="#303030", linespacing=1.5,
            bbox=dict(boxstyle="round,pad=0.4", fc="#fff8f8", ec="#d32f2f", lw=0.9),
        )

    xs = list(range(0, len(sub), 6))
    axs[-1].set_xticks(xs)
    axs[-1].set_xticklabels(
        [pd.Timestamp(sub["datetime"].iloc[i]).strftime("%d %b\n%H:%M") for i in xs], fontsize=8
    )
    fig.suptitle(
        f"{strategy_name} · {case} · {direction} · вход {ta['datetime'].iloc[entry_i]}",
        fontsize=12, y=0.995,
    )
    fig.subplots_adjust(right=0.72)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, format="svg", bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="Генерация SVG-картинок сигналов стратегий.")
    parser.add_argument("--strategy", required=True, choices=sorted(STRATEGY_CONFIGS), help="Имя стратегии из реестра")
    parser.add_argument("--case", required=True, help="Имя snapshot-кейса (папка в tests/snapshot/data)")
    parser.add_argument("--direction", choices=["BUY", "SELL"], default=None, help="Направление события")
    parser.add_argument("--out", default=None, help="Папка вывода (по умолчанию openspec/assets/signals)")
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else ASSETS_DIR
    directions = [args.direction] if args.direction else ["BUY", "SELL"]
    for direction in directions:
        out = out_dir / f"{args.strategy}_{direction}.svg"
        render(args.strategy, args.case, direction, out)
        print(f"Сохранено: {out}")


if __name__ == "__main__":
    main()