"""
Read-only проба совместимости live-ingestion с брокером (задача 8.5).

Только чтение: каталог, свечи, подписка на свечи, расписание, статус и обезличенные
сделки. Заявки не создаются, рабочие базы робота не открываются. Токен берётся из
.env и в отчёт не попадает. Каждый раздел изолирован: сбой одного не мешает другим.

Без --live-minutes проверяется то, что не зависит от торгов (подходит для
закрытой биржи). С --live-minutes N поток слушается N минут и измеряются
задержка публикации, совпадение stream и GetCandles и коррекции — запускать в
торговую сессию.

Пример:
    python tools/ingestion_probe.py
    python tools/ingestion_probe.py --tickers SBER:share NGZ6:future --live-minutes 20

Отчёт: data/probe/ingestion-probe-<время>.json и краткая сводка в консоли.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_TICKERS = ("SBER:share", "GAZP:share")
EDGE_TIMEFRAMES = ("1m", "15m", "1h")
LOOKBACK = {"1m": timedelta(days=1), "15m": timedelta(days=1), "1h": timedelta(days=7)}


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    return str(value)


def price(quotation):
    return quotation.units + quotation.nano / 1e9


def enum_name(value):
    return getattr(value, "name", value)


def section(report, name, func, *args, **kwargs):
    started = time.monotonic()
    try:
        report[name] = {"ok": True, **func(*args, **kwargs)}
    except Exception as exc:  # раздел изолирован: фиксируем и идём дальше
        report[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    report[name]["seconds"] = round(time.monotonic() - started, 3)


def resolve(client, tickers):
    from t_tech.invest import InstrumentIdType
    from src.api.instruments import find_working_instrument
    resolved = []
    for item in tickers:
        ticker, _, kind = item.partition(":")
        uid = find_working_instrument(client, ticker, kind or "share")
        card = client.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID, id=uid).instrument
        resolved.append({"ticker": ticker, "type": kind or "share", "uid": uid,
                         "exchange": getattr(card, "exchange", ""), "class_code": getattr(card, "class_code", "")})
    return resolved


# ── поток: ACK, ping, членство, свечи ──
def probe_stream(session_factory, instruments, *, listen_seconds, check_after):
    from src.api.client import NATIVE_CANDLE_INTERVALS
    from src.scheduler.timing import CandleScheduler

    pairs = [(item["uid"], tf) for item in instruments for tf in NATIVE_CANDLE_INTERVALS]
    session = session_factory()
    received = []
    errors = []

    def reader():
        try:
            for response in session:
                received.append((time.monotonic(), utc_now(), response))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    opened = time.monotonic()
    session.open()
    session.configure_ping(15000)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    sent_at = time.monotonic()
    session.subscribe(pairs)
    checked_at = None
    try:
        while time.monotonic() - sent_at < listen_seconds:
            if checked_at is None and time.monotonic() - sent_at >= check_after:
                checked_at = time.monotonic()
                session.subscriptions()
            time.sleep(0.2)
    finally:
        session.close()
        thread.join(5)
    acks, membership, pings, candles = {}, [], [], []
    for monotonic, wall, response in received:
        if response.kind == "subscribe_candles_response":
            for ack in response.acknowledgements:
                entry = {"status": ack.status, "action": ack.action, "waiting_close": ack.waiting_close,
                         "source_echo": ack.source, "latency_s": round(monotonic - sent_at, 3)}
                key = f"{ack.uid}:{ack.timeframe}"
                if key not in acks:
                    acks[key] = entry  # первый ACK пары — подтверждение подписки
                elif checked_at is not None and monotonic >= checked_at:
                    membership.append(key)  # повторный после проверки — членство
        elif response.kind == "ping":
            pings.append(round(monotonic - opened, 3))
        elif response.kind == "candle" and response.candle is not None:
            candle = response.candle
            close = CandleScheduler(candle.timeframe).bar_close(candle.open_time.replace(tzinfo=None)) if candle.timeframe else None
            candles.append({"uid": candle.uid, "timeframe": candle.timeframe, "open": iso(candle.open_time),
                            "last_trade": iso(candle.last_trade_time), "received": iso(wall),
                            "received_after_close_s": None if close is None else round(
                                (wall.replace(tzinfo=None) - close).total_seconds(), 3),
                            "ohlcv": [float(value) for value in candle.prices] + [candle.volume],
                            "source": candle.source})
    missing = [f"{uid}:{tf}" for uid, tf in pairs if f"{uid}:{tf}" not in acks]
    return {"pairs": len(pairs), "acks": acks, "missing_acks": missing, "membership_reported": sorted(set(membership)),
            "ping_offsets_s": pings, "candles": candles, "reader_errors": errors,
            "events_per_second_peak": max(Counter(int(m) for m, _, _ in received).values(), default=0)}


def compare_stream_with_rpc(client, candles):
    """Совпадение закрытых stream-свечей с GetCandles и коррекции одного ключа."""
    from src.api.client import LIVE_CANDLE_SOURCE, NATIVE_CANDLE_INTERVALS
    from src.scheduler.timing import CandleScheduler

    versions = defaultdict(list)
    for candle in candles:
        versions[candle["uid"], candle["timeframe"], candle["open"]].append(candle["ohlcv"])
    corrections = {":".join(key): values for key, values in versions.items() if len({tuple(v) for v in values}) > 1}
    mismatches, compared = [], 0
    for (uid, tf, opened), values in list(versions.items())[:60]:
        start = datetime.fromisoformat(opened)
        end = CandleScheduler(tf).bar_close(start.replace(tzinfo=None)).replace(tzinfo=timezone.utc)
        response = client.market_data.get_candles(instrument_id=uid, from_=start, to=end,
            interval=NATIVE_CANDLE_INTERVALS[tf].rpc, candle_source_type=LIVE_CANDLE_SOURCE)
        rows = [c for c in response.candles if c.time == start]
        compared += 1
        if not rows:
            mismatches.append({"key": f"{uid}:{tf}:{opened}", "rpc": None, "stream": values[-1]})
            continue
        rpc = [price(rows[0].open), price(rows[0].high), price(rows[0].low), price(rows[0].close), rows[0].volume]
        if [round(v, 9) for v in rpc] != [round(v, 9) for v in values[-1]]:
            mismatches.append({"key": f"{uid}:{tf}:{opened}", "rpc": rpc, "stream": values[-1],
                               "rpc_is_complete": rows[0].is_complete})
    return {"compared": compared, "mismatches": mismatches, "corrections": corrections}


# ── GetCandles: границы окна, лимит, источники ──
def probe_edges(client, instrument):
    from src.api.client import LIVE_CANDLE_SOURCE, NATIVE_CANDLE_INTERVALS

    result = {}
    now = utc_now()
    for tf in EDGE_TIMEFRAMES:
        interval = NATIVE_CANDLE_INTERVALS[tf].rpc
        history = client.market_data.get_candles(instrument_id=instrument["uid"], from_=now - LOOKBACK[tf], to=now,
                                                 interval=interval, candle_source_type=LIVE_CANDLE_SOURCE).candles
        closed = [c for c in history if c.is_complete]
        if len(closed) < 12:
            result[tf] = {"note": "мало закрытых свечей за период", "count": len(closed)}
            continue
        anchor = closed[len(closed) // 2].time
        step = closed[len(closed) // 2 + 1].time - anchor
        upper = client.market_data.get_candles(instrument_id=instrument["uid"], from_=anchor - 5 * step, to=anchor,
                                               interval=interval, candle_source_type=LIVE_CANDLE_SOURCE).candles
        lower = client.market_data.get_candles(instrument_id=instrument["uid"], from_=anchor, to=anchor + 5 * step,
                                               interval=interval, candle_source_type=LIVE_CANDLE_SOURCE).candles
        limited = client.market_data.get_candles(instrument_id=instrument["uid"], from_=now - LOOKBACK[tf], to=now,
                                                 interval=interval, candle_source_type=LIVE_CANDLE_SOURCE, limit=10).candles
        result[tf] = {"window_count": len(history), "incomplete_in_window": len(history) - len(closed),
                      "anchor": iso(anchor),
                      "upper_bound_inclusive": any(c.time == anchor for c in upper),
                      "lower_bound_inclusive": any(c.time == anchor for c in lower),
                      "limit_10_returned": len(limited),
                      "limit_10_first": iso(limited[0].time) if limited else None,
                      "limit_10_last": iso(limited[-1].time) if limited else None}
    return result


def probe_sources(client, instrument):
    from t_tech.invest.schemas import CandleSource
    from src.api.client import NATIVE_CANDLE_INTERVALS

    now = utc_now()
    found = {}
    for source in (CandleSource.CANDLE_SOURCE_UNSPECIFIED, CandleSource.CANDLE_SOURCE_EXCHANGE,
                   CandleSource.CANDLE_SOURCE_INCLUDE_WEEKEND):
        candles = client.market_data.get_candles(instrument_id=instrument["uid"], from_=now - timedelta(days=1), to=now,
            interval=NATIVE_CANDLE_INTERVALS["1m"].rpc, candle_source_type=source).candles
        found[source.name] = {iso(c.time): ([price(c.open), price(c.high), price(c.low), price(c.close), c.volume],
                                            enum_name(c.candle_source)) for c in candles}
    base, exchange = found["CANDLE_SOURCE_UNSPECIFIED"], found["CANDLE_SOURCE_EXCHANGE"]
    return {"counts": {name: len(rows) for name, rows in found.items()},
            "unspecified_actual_sources": dict(Counter(source for _, source in base.values())),
            "only_in_unspecified": len(set(base) - set(exchange)),
            "only_in_exchange": len(set(exchange) - set(base)),
            "value_differences": sum(1 for key in set(base) & set(exchange) if base[key][0] != exchange[key][0])}


# ── расписание, статус, обезличенные сделки ──
def describe_day(day):
    fields = {}
    for name in ("date", "start_time", "end_time", "opening_auction_start_time", "opening_auction_end_time",
                 "closing_auction_start_time", "closing_auction_end_time", "clearing_start_time", "clearing_end_time",
                 "evening_opening_auction_start_time", "evening_start_time", "evening_end_time",
                 "premarket_start_time", "premarket_end_time"):
        value = getattr(day, name, None)
        if value is not None and getattr(value, "year", 1970) > 1970:
            fields[name] = iso(value)
    fields["date_raw"] = repr(getattr(day, "date", None))
    fields["is_trading_day"] = getattr(day, "is_trading_day", None)
    fields["intervals"] = [{"type": item.type, "start": iso(item.interval.start_ts), "end": iso(item.interval.end_ts)}
                           for item in getattr(day, "intervals", ())]
    return fields


def probe_schedule(client, instrument):
    now = utc_now()
    result = {"exchange": instrument["exchange"]}
    # Брокер отклоняет `from` раньше текущей даты: текущее окно — от момента
    # запуска; запрос за вчера оставлен как явная проверка этого ограничения.
    for label, start, end in (("current", now, now + timedelta(days=7)),
                              ("yesterday_rejected", now - timedelta(days=1), now + timedelta(days=1))):
        try:
            response = client.instruments.trading_schedules(exchange=instrument["exchange"], from_=start, to=end)
            result[label] = [{"exchange": schedule.exchange, "days": [describe_day(day) for day in schedule.days]}
                             for schedule in response.exchanges]
        except Exception as exc:
            result[label] = {"error": f"{type(exc).__name__}: {exc}"}
    status = client.market_data.get_trading_status(instrument_id=instrument["uid"])
    result["trading_status"] = {"status": enum_name(status.trading_status),
                                "api_trade_available": status.api_trade_available_flag}
    trades = client.market_data.get_last_trades(instrument_id=instrument["uid"], from_=now - timedelta(hours=1), to=now)
    result["last_hour_trades"] = len(trades.trades)
    return result


# ── доказательство отсутствия сделок ──
def probe_no_trade_evidence(client, instrument, *, minutes=55):
    """Минуты со свечой против минут со сделками GetLastTrades за последний час."""
    from src.api.client import LIVE_CANDLE_SOURCE, NATIVE_CANDLE_INTERVALS

    end = utc_now().replace(second=0, microsecond=0)
    start = end - timedelta(minutes=minutes)
    candles = client.market_data.get_candles(instrument_id=instrument["uid"], from_=start, to=end,
        interval=NATIVE_CANDLE_INTERVALS["1m"].rpc, candle_source_type=LIVE_CANDLE_SOURCE).candles
    with_candle = {c.time.replace(tzinfo=timezone.utc) for c in candles if c.is_complete}
    trades = client.market_data.get_last_trades(instrument_id=instrument["uid"], from_=start, to=end).trades
    with_trades = {t.time.replace(second=0, microsecond=0, tzinfo=timezone.utc) for t in trades}
    window = [start + timedelta(minutes=index) for index in range(minutes)]
    candle_only = [iso(m) for m in window if m in with_candle and m not in with_trades]
    trades_only = [iso(m) for m in window if m in with_trades and m not in with_candle]
    return {"minutes": minutes, "trades": len(trades),
            "first_trade": iso(min((t.time for t in trades), default=None)),
            "with_candle": sum(1 for m in window if m in with_candle),
            "with_trades": sum(1 for m in window if m in with_trades),
            "neither": sum(1 for m in window if m not in with_candle and m not in with_trades),
            "candle_without_trades": candle_only, "trades_without_candle": trades_only,
            "agrees": not candle_only and not trades_only}


# ── холодный старт ──
def probe_cold_start(instrument, *, days):
    from src.data.loader import load_candles
    from src.data.request_budget import RequestBudget

    def rss_mb():
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(value / (1024 * 1024) if sys.platform == "darwin" else value / 1024, 1)

    budget = RequestBudget(lambda: datetime.now(timezone.utc).replace(tzinfo=None), 0, per_minute=120)
    before, started = rss_mb(), time.monotonic()
    frame, _ = load_candles(instrument["ticker"], instrument["type"], "1m",
                            start_date=utc_now() - timedelta(days=days), instrument_id=instrument["uid"],
                            request_budget=budget, single_attempt=False)
    return {"days": days, "rows": len(frame), "rpc_calls": budget.calls,
            "seconds": round(time.monotonic() - started, 2), "max_rss_mb_before": before, "max_rss_mb_after": rss_mb()}


def run_probe(client, session_factory, tickers, *, listen_seconds, check_after, live_minutes, cold_days,
              no_trade_only=False):
    report = {"started_at": iso(utc_now()), "tickers": list(tickers),
              "mode": "no-trade-evidence" if no_trade_only else "live" if live_minutes else "closed-market-safe"}
    section(report, "instruments", lambda: {"items": resolve(client, tickers)})
    instruments = report["instruments"].get("items", [])
    if not instruments:
        return report
    for item in instruments:
        section(report, f"no_trade:{item['ticker']}", probe_no_trade_evidence, client, item)
    if no_trade_only:
        report["finished_at"] = iso(utc_now())
        return report
    listen = max(listen_seconds, live_minutes * 60)
    section(report, "stream", probe_stream, session_factory, instruments, listen_seconds=listen, check_after=check_after)
    if live_minutes and report["stream"].get("candles"):
        section(report, "stream_vs_rpc", compare_stream_with_rpc, client, report["stream"]["candles"])
    for item in instruments:
        section(report, f"edges:{item['ticker']}", probe_edges, client, item)
        section(report, f"schedule:{item['ticker']}", probe_schedule, client, item)
    section(report, f"sources:{instruments[0]['ticker']}", probe_sources, client, instruments[0])
    section(report, f"cold_start:{instruments[0]['ticker']}", probe_cold_start, instruments[0], days=cold_days)
    report["finished_at"] = iso(utc_now())
    return report


def summary(report):
    lines = [f"Режим: {report['mode']}"]
    stream = report.get("stream", {})
    if stream.get("ok"):
        acks = stream["acks"]
        good = sum(1 for ack in acks.values() if ack["status"] == 1 and ack["waiting_close"])
        lines.append(f"Подписки: подтверждено {good}/{stream['pairs']}, без ACK: {len(stream['missing_acks'])}, "
                     f"ping: {len(stream['ping_offsets_s'])}, свечей: {len(stream['candles'])}")
    for name, value in report.items():
        if name.startswith("no_trade:") and value.get("ok"):
            lines.append(f"{name}: свеча {value['with_candle']}, сделки {value['with_trades']}, ни того ни другого "
                         f"{value['neither']} из {value['minutes']} минут; совпадает: {'да' if value['agrees'] else 'нет'}")
    for name, value in report.items():
        if isinstance(value, dict) and value.get("ok") is False:
            lines.append(f"Ошибка раздела {name}: {value['error']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only проба live-ingestion (8.5)")
    parser.add_argument("--tickers", nargs="+", default=list(DEFAULT_TICKERS),
                        help="ТИКЕР:тип, тип share|future|etf (по умолчанию SBER и GAZP)")
    parser.add_argument("--listen-seconds", type=int, default=30, help="сколько слушать поток без --live-minutes")
    parser.add_argument("--live-minutes", type=int, default=0, help="слушать поток N минут (в торговую сессию)")
    parser.add_argument("--cold-days", type=int, default=30, help="глубина холодного старта 1m, дней")
    parser.add_argument("--no-trade-only", action="store_true",
                        help="только проверка минут без сделок по GetLastTrades (секунды, в торговую сессию)")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "probe")
    args = parser.parse_args(argv)

    from src.api.client import TinkoffClientProvider
    from src.config import TINKOFF_TOKEN

    if not TINKOFF_TOKEN:
        print("Нет TINKOFF_TOKEN в .env — проба не запущена.")
        return 2
    provider = TinkoffClientProvider()
    with provider.client_context() as client:
        report = run_probe(client, provider.stream_session, args.tickers, listen_seconds=args.listen_seconds,
                           check_after=min(10, args.listen_seconds // 2), live_minutes=args.live_minutes,
                           cold_days=args.cold_days, no_trade_only=args.no_trade_only)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"ingestion-probe-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(summary(report))
    print(f"Отчёт: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
