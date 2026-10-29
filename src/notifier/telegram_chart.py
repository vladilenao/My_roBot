"""Семантическая сцена и headless PNG: импорт графики только в renderer."""
from __future__ import annotations

from bisect import bisect_right
from datetime import timedelta
from io import BytesIO
from math import ceil

from src.events.types import EventType
from src.events.visual import breaks_of, serializable, utc
from src.notifier.telegram_templates import chart_notes, money, price, quantity, terminal, timeframe_label
from src.scheduler.timing import CandleScheduler

GREEN, RED, BLUE = "#36d399", "#ff7185", "#73baff"
BACKGROUND = "#101827"
GRID, MUTED = "#3a4558", "#8794a7"
MAX_DAY_SEPARATORS = 10


class CandleAxis:
    """Ось по номерам свечей окна: время между свечами места не занимает.

    Свеча ``i`` занимает [i − 0.5, i + 0.5); момент внутри свечи — пропорционально,
    момент в разрыве — на границе разрыва, момент после последней свечи
    продолжает время её периодами (правое поле графика).
    """

    def __init__(self, candles, timeframe):
        grid = CandleScheduler(timeframe)
        self.opens = [utc(candle["time"]) for candle in candles]
        self.closes = [grid.bar_close(opened) for opened in self.opens]

    def index(self, stamp):
        return self.opens.index(utc(stamp))

    def position(self, stamp):
        moment = utc(stamp)
        i = bisect_right(self.opens, moment) - 1
        if i < 0:
            return -.5
        opened, closed = self.opens[i], self.closes[i]
        if moment < closed:
            return i - .5 + (moment - opened) / (closed - opened)
        if i == len(self.opens) - 1:
            return i + .5 + (moment - closed) / (closed - opened)
        return i + .5

    def day_starts(self, offset):
        local = [(opened + offset).date() for opened in self.opens]
        return [i for i in range(1, len(local)) if local[i] != local[i - 1]]

    def ticks(self, offset, *, most=8):
        """Номера подписанных свечей: начало дня и равномерный шаг между ними."""
        count = len(self.opens)
        step = max(1, ceil(count / most))
        starts = self.day_starts(offset)
        if len(starts) > most:
            return list(range(0, count, step)), set(range(count))
        # Первая свеча подписывается датой, если рядом нет начала дня.
        anchors = starts if starts and starts[0] < step / 2 else [0] + starts
        regular = [i for i in range(0, count, step) if all(abs(i - anchor) >= step / 2 for anchor in anchors)]
        return sorted(set(anchors) | set(regular)), set(anchors)


def compact_png(photo: bytes, max_bytes: int) -> bytes:
    """Уменьшить PNG без обрезки; слишком плотный график отдаётся текстом."""
    if len(photo) <= max_bytes:
        return photo
    from PIL import Image, ImageColor

    with Image.open(BytesIO(photo)) as image:
        rgb = image.convert("RGB")
    # Палитра до resize исключает новые оттенки сглаживания и уменьшает PNG.
    # Обязательные цвета не должны исчезать из-за малой площади стопа/входа.
    fixed = [channel for color in (BACKGROUND, GREEN, RED, BLUE, "#ffffff", "#b9c4d5", "#8794a7", "#3a4558")
             for channel in ImageColor.getrgb(color)]
    palettes = []
    for count in (64, 32, 16):
        adaptive = rgb.quantize(colors=count - 8, dither=Image.Dither.NONE)
        palette = Image.new("P", (1, 1))
        palette.putpalette(fixed + adaptive.getpalette()[:(count - 8) * 3])
        palettes.append(rgb.quantize(palette=palette, dither=Image.Dither.NONE))
    minimum = min(800, rgb.width)
    width = rgb.width
    while True:
        size = (width, max(1, round(rgb.height * width / rgb.width)))
        for palette in palettes:
            reduced = palette if size == rgb.size else palette.resize(size, Image.Resampling.NEAREST)
            with BytesIO() as stream:
                reduced.save(stream, format="PNG", optimize=True)
                candidate = stream.getvalue()
            if len(candidate) <= max_bytes:
                return candidate
        if width == minimum:
            raise ValueError("График не помещается в лимит при ширине не менее 800 px")
        width = max(minimum, int(width * .85))


def build_scene(event, tz_offset_hours=0):
    visual = event.get("visual")
    if not visual or not visual["market"]["candles"]:
        return None
    stage = "plan" if event.type is EventType.SIGNAL else "final" if terminal(event) else "stop" if event.type is EventType.STOP_MOVED else None
    if stage is None:
        return None
    plan, state = visual["plan"], visual["state"]
    step, unit = visual.get("price_step"), visual["unit"]
    levels = [{"price": plan["entry"], "label": "Вход " + price(plan["entry"], step), "kind": "entry"}]
    stop = plan["stop"] if stage == "plan" else state.get("stop")
    if stop is not None:
        levels.append({"price": stop, "label": "СТОП " + price(stop, step), "kind": "stop",
                       "from_time": (visual["stops"][-1]["time"] if visual["stops"] else visual["fills"][0]["time"] if visual["fills"] else visual["as_of"]) if stage != "plan" else None})
    targets = plan["targets"] if stage == "plan" else state["targets"]
    for target in targets:
        if target["quantity"]:
            levels.append({"price": target["price"], "label": f"ЦЕЛЬ{target['number']} {price(target['price'], step)} · {quantity(target['quantity'], unit)}",
                           "kind": "target"})
    markers = []
    before_window = []
    candles = visual["market"]["candles"]
    first = utc(candles[0]["time"])
    for fill in visual["fills"] if stage != "plan" else ():
        label = {"OPEN": "Вход", "ADD": "Добор", "STOP": "СТОП", "CLOSE": "Выход", "REDUCE": "Частичный выход"}.get(fill["role"], fill["role"])
        marker = {"time": fill["time"], "price": fill["price"], "side": fill["side"],
                  "label": f"{label} · {quantity(fill['quantity'], unit)}"}
        (before_window if utc(fill["time"]) < first else markers).append(marker)
    stops = []
    if stage != "plan":
        retained = visual["stops"]
        boundary = visual["market"].get("boundary_stop")
        if boundary is None and not visual["market"]["stop_history_limited"]:
            boundary = retained[0].get("old") if retained else state.get("stop")
        if boundary is not None:
            first_fill = visual["fills"][0]["time"] if visual["fills"] else visual["as_of"]
            start = max(first, utc(first_fill)) if not visual["market"]["history_limited"] else first
            stops.append({"time": start.isoformat(), "price": boundary})
        for stop_fact in retained:
            if utc(stop_fact["time"]) >= first:
                if visual["market"]["stop_history_limited"] and not stops:
                    stops.append({"time": stop_fact["time"], "price": stop_fact["new"]})
                else:
                    stops.append({"time": stop_fact["time"], "price": stop_fact["new"]})
    financial = visual["financial"]
    result = None
    if stage == "final":
        if financial.get("net") is not None and financial["fees_known"] and financial["units"] == "RUB":
            estimated = financial["fees_source"] != "broker"
            result = ("ИТОГ (с оценкой) " if estimated else "ИТОГ ") + money(financial["net"], signed=True)
        else:
            result = "ИТОГ ПОСЛЕ КОМИССИЙ НЕИЗВЕСТЕН"
    return serializable({"stage": stage, "instrument": visual["instrument"], "side": visual["side"],
                         "timeframe": visual["timeframe"], "tz_offset_hours": tz_offset_hours,
                         "as_of": visual["as_of"], "candles": candles, "levels": levels,
                         "original_stop": plan["stop"], "markers": markers, "before_window": before_window,
                         "stop_steps": stops, "result": result, "financial": financial,
                         "breaks": breaks_of(visual["market"], visual["timeframe"]),
                         "notes": chart_notes(visual, tz_offset_hours)})


def render_png(scene):
    if scene is None:
        return None
    # No pyplot/global figure registry/GUI backend; used only by the worker.
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FixedLocator, FuncFormatter
    from matplotlib.font_manager import FontProperties

    def font(size=12, weight="normal"):
        return FontProperties(family="DejaVu Sans", size=size, weight=weight)
    height = max(8, len(scene["levels"]) * .24)
    if 1440 + height * 120 > 10000:
        raise ValueError("Слишком много уровней для читаемой фотографии Telegram")
    figure = Figure(figsize=(12, height), dpi=120, facecolor=BACKGROUND)
    FigureCanvasAgg(figure)
    try:
        ax = figure.add_subplot(111, facecolor=BACKGROUND)
        offset = timedelta(hours=scene["tz_offset_hours"])
        candles = scene["candles"]
        axis = CandleAxis(candles, scene["timeframe"])
        x = axis.position
        period = 1
        left = 0
        last = max(len(candles) - 1, x(scene["as_of"]))
        right = last + period * 9
        prices = [float(c[k]) for c in candles for k in ("low", "high")]
        prices += [float(level["price"]) for level in scene["levels"]]
        prices += [float(m["price"]) for m in scene["markers"]]
        prices += [float(scene["original_stop"])] + [float(s["price"]) for s in scene["stop_steps"]]
        span = max(max(prices) - min(prices), max(prices) * .001)
        for stamp, candle in enumerate(candles):
            opening, close, high, low = (float(candle[k]) for k in ("open", "close", "high", "low"))
            color = GREEN if close >= opening else RED
            ax.plot([stamp, stamp], [low, high], color=color, linewidth=1.4)
            ax.add_patch(Rectangle((stamp - period * .29, min(opening, close)), period * .58,
                                   max(abs(close-opening), span * .005), color=color))
        colors = {"entry": BLUE, "stop": RED, "target": GREEN}
        # Separate nearly coincident labels while keeping the true price line.
        labels = []
        for level in sorted(scene["levels"], key=lambda item: float(item["price"])):
            actual = float(level["price"])
            label_gap = span * .04 * 8 / height
            label_y = max(actual + span * .012, labels[-1] + label_gap) if labels else actual + span * .012
            labels.append(label_y)
            color = colors[level["kind"]]
            if level.get("from_time"):
                ax.plot([max(left, x(level["from_time"])), right], [actual, actual], color=color, linestyle="--", linewidth=1.2, alpha=.8)
            else:
                ax.axhline(actual, color=color, linestyle="--", linewidth=1.2, alpha=.8)
            ax.annotate(level["label"], xy=(right, actual), xytext=(right, label_y), ha="right", va="bottom", color=color,
                    fontproperties=font(13, "bold"), arrowprops={"arrowstyle": "-", "color": color, "linewidth": .5},
                    bbox={"facecolor": BACKGROUND, "edgecolor": "none", "alpha": .93})
        if scene["stage"] != "plan":
            ax.axhline(float(scene["original_stop"]), color="#8794a7", linestyle=":", alpha=.7)
            steps = scene["stop_steps"]
            if steps:
                ax.step([x(s["time"]) for s in steps] + [last + period],
                        [float(s["price"]) for s in steps] + [float(steps[-1]["price"])],
                        where="post", color=RED, linewidth=2.2)
            for i, marker in enumerate(scene["markers"]):
                mx, my = x(marker["time"]), float(marker["price"])
                is_buy = marker["side"] == "BUY"
                color = BLUE if marker["label"].startswith(("Вход", "Добор")) else RED if marker["label"].startswith("СТОП") else GREEN
                ax.scatter([mx], [my], marker="^" if is_buy else "v", s=100, color=color, edgecolor="white", zorder=8)
                if len(scene["markers"]) <= 12:
                    ax.annotate(marker["label"], (mx, my), xytext=(-15, 25 + 16*(i % 2)), textcoords="offset points",
                                ha="right", color=color, fontproperties=font(11),
                                arrowprops={"arrowstyle": "->", "color": color})
        titles = {"plan": "ПЛАН ВХОДА В СДЕЛКУ", "stop": "СТОП ПЕРЕНЕСЁН И ПОДТВЕРЖДЁН", "final": "СДЕЛКА ЗАКРЫТА"}
        direction = "ПОКУПКА" if scene["side"] == "BUY" else "ПРОДАЖА"
        ax.set_title(f"{scene['instrument']}   {direction}   {timeframe_label(scene['timeframe'])}\n{titles[scene['stage']]}",
                     loc="left", color="white", fontproperties=font(18, "bold"), pad=24)
        if scene.get("result"):
            finance = scene["financial"]
            subtitle = "До комиссии " + money(finance.get("gross"), signed=True, units=finance["units"])
            if finance["fees_known"]:
                subtitle += " · комиссии " + money(finance.get("fees"))
            ax.text(.015, .97, scene["result"] + "\n" + subtitle, transform=ax.transAxes, va="top",
                    color="white", fontproperties=font(16, "bold"),
                    bbox={"facecolor": "#20364b", "edgecolor": "none", "boxstyle": "round,pad=.6"})
        ax.set_xlim(left - period, right + period * .2)
        ax.set_ylim(min(prices) - span * .12, max(max(prices) + span * .18, labels[-1] + span * .1))
        starts = axis.day_starts(offset)
        if len(starts) <= MAX_DAY_SEPARATORS:
            for start in starts:
                ax.axvline(start - .5, color=MUTED, linewidth=1, alpha=.55)
        unavailable_labelled = False
        for gap in scene["breaks"]:
            boundary = axis.index(gap["after"]) + .5
            if gap["kind"] == "missing":
                ax.axvspan(boundary - .15, boundary + .15, color=RED, alpha=.35, linewidth=0)
                ax.text(boundary, .985, f"пропуск {gap['missing']}", transform=ax.get_xaxis_transform(), ha="center",
                        va="top", color=RED, fontproperties=font(10, "bold"))
            elif gap["kind"] == "schedule_unavailable":
                ax.axvline(boundary, color=MUTED, linestyle="--", linewidth=1, alpha=.85)
                if not unavailable_labelled:
                    ax.text(boundary, .985, "расписание недоступно", transform=ax.get_xaxis_transform(), ha="center",
                            va="top", color=MUTED, fontproperties=font(10))
                    unavailable_labelled = True
        ticks, dated = axis.ticks(offset)

        def tick_label(value, _):
            i = round(value)
            if not 0 <= i < len(candles) or abs(value - i) > 1e-6:
                return ""
            return (axis.opens[i] + offset).strftime("%d.%m\n%H:%M" if i in dated else "%H:%M")
        ax.xaxis.set_major_locator(FixedLocator(ticks))
        ax.xaxis.set_major_formatter(FuncFormatter(tick_label))
        ax.tick_params(colors="#b9c4d5", labelsize=11)
        ax.grid(color=GRID, alpha=.28)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.set_xlabel(f"Время UTC{scene['tz_offset_hours']:+g} · неторговое время скрыто", color="#b9c4d5", fontproperties=font(12))
        note = (" · ".join(scene["notes"]) or "План и подтверждённые исполнения")
        note = note[:1].upper() + note[1:]
        if scene["before_window"]:
            note += f" · исполнений раньше окна: {len(scene['before_window'])} (цены и время — в подписи)"
        figure.text(.08, .025, note, color="#8794a7", fontproperties=font(10))
        figure.subplots_adjust(left=.09, right=.95, top=.82, bottom=.14)
        with BytesIO() as stream:
            figure.savefig(stream, format="png", facecolor=BACKGROUND)
            return stream.getvalue()
    finally:
        figure.clear()


def smoke():
    """Автономная проверка поставки Agg/fonts. Не запускает бота или HTTP."""
    scene = {"stage": "plan", "instrument": "Проверка кириллицы", "side": "BUY", "timeframe": "1m",
             "tz_offset_hours": 3, "as_of": "2026-01-01T00:01:00", "original_stop": "99",
             "candles": [{"time": "2026-01-01T00:00:00", "open": "100", "high": "102", "low": "99", "close": "101"}],
             "levels": [{"price": "100", "label": "Вход · 1 лот", "kind": "entry"},
                        {"price": "99", "label": "СТОП 99", "kind": "stop"},
                        {"price": "103", "label": "ЦЕЛЬ1 103", "kind": "target"}],
             "markers": [], "before_window": [], "stop_steps": [], "result": None, "financial": {},
             "breaks": [], "notes": []}
    png = render_png(scene)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("PNG недоступен")
    print(f"График Telegram: Agg и кириллица доступны, PNG {len(png)} байт.")
