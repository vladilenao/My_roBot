"""Отчёт кластерного фильтра за день или период (design D9, ``GET /api/v1/clusters/report``).

Только читает: вердикты — трассы ``filter.cluster`` в ``calculations``,
сделки — по ``signal_id``, движение цены — 1m свечи локального журнала.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import median
from typing import Callable

import numpy as np
import pandas as pd

from src.clusters.verdict import ALGORITHM, CONFIRM, NEUTRAL, UNKNOWN, VETO, VETO_STRONG

HORIZONS = (15, 60)
GROUP_ORDER = (VETO_STRONG, VETO, CONFIRM, NEUTRAL, UNKNOWN)
MSK = timedelta(hours=3)
MINUTE = timedelta(minutes=1)
STALE = timedelta(minutes=15)
MAX_PERIOD = 366
PATH = "/api/v1/clusters/report"
UidCandles = Callable[[str], pd.DataFrame]


class ReportError(ValueError):
    """Неверные параметры отчёта: код и причина для ответа 400."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _money(value: Decimal) -> dict[str, str]:
    return {"amount": str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), "currency": "RUB"}


def _decimal(value: float, places: int = 2) -> str:
    return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _value(data: dict, key: str):
    item = data.get(key)
    return item.get("value") if isinstance(item, dict) else None


def _utc(text: str) -> datetime:
    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


class ClusterReport:
    """Сборщик отчёта; ``trades_db`` открывается только на чтение."""

    def __init__(self, trades_db: str | Path, candles: UidCandles, *, configured_mode: str = "off",
                 clock: Callable[[], datetime] | None = None) -> None:
        self._trades_db = Path(trades_db)
        self._candles = candles
        self._configured_mode = configured_mode
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def parse(self, raw_date: str | None, raw_period: str | None) -> tuple[date, int]:
        """Проверка ``date`` и ``period`` запроса."""
        if raw_date is None:
            raise ReportError("BAD_PERIOD", "Параметр date обязателен (ГГГГ-ММ-ДД).")
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            raise ReportError("BAD_PERIOD", "Параметр date — дата в формате ГГГГ-ММ-ДД.") from None
        try:
            period = 1 if raw_period is None else int(raw_period)
        except ValueError:
            raise ReportError("BAD_PERIOD", "Параметр period — целое число дней.") from None
        if not 1 <= period <= MAX_PERIOD:
            raise ReportError("BAD_PERIOD", f"Параметр period должен быть от 1 до {MAX_PERIOD}.")
        if day > self._today():
            raise ReportError("BAD_PERIOD", "Дата отчёта позже сегодняшней.")
        return day, period

    def _today(self) -> date:
        return (self._clock().astimezone(timezone.utc) + MSK).date()

    def build(self, day: date, period: int) -> dict:
        start = day - timedelta(days=period - 1)
        now = self._clock().astimezone(timezone.utc)
        rows = self._verdicts(start, day)
        candles: dict[str, pd.DataFrame] = {}
        signals = [self._signal(row, now, candles) for row in rows]
        modes = {item["_mode"] for item in signals}
        mode = modes.pop() if len(modes) == 1 else ("mixed" if modes else self._configured_mode)
        groups = [self._group(name, [s for s in signals if s["verdict"] == name])
                  for name in GROUP_ORDER if any(s["verdict"] == name for s in signals)]
        vetoed = [s for s in signals if s["verdict"] in (VETO_STRONG, VETO) and s["_closed_pnl"] is not None]
        winners = [s["_closed_pnl"] for s in vetoed if s["_closed_pnl"] > 0]
        losers = [s["_closed_pnl"] for s in vetoed if s["_closed_pnl"] < 0]
        removed = sum((s["_closed_pnl"] for s in vetoed), Decimal("0"))
        links = {"self": {"href": f"{PATH}?date={day.isoformat()}&period={period}"},
                 "previous": {"href": f"{PATH}?date={(start - timedelta(days=1)).isoformat()}&period={period}"}}
        if day + timedelta(days=1) <= self._today():
            links["next"] = {"href": f"{PATH}?date={(day + timedelta(days=period)).isoformat()}&period={period}"}
        return {
            "period": {"from": start.isoformat(), "to": day.isoformat(), "days": period},
            "mode": mode,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "horizons_min": list(HORIZONS),
            "groups": groups,
            "veto_effect": {
                "includes": [VETO_STRONG, VETO],
                "trades_removed": len(vetoed),
                "winners_removed": len(winners),
                "winners_net_pnl": _money(sum(winners, Decimal("0"))),
                "losers_removed": len(losers),
                "losers_net_pnl": _money(sum(losers, Decimal("0"))),
                "net_effect": _money(-removed),
            },
            "signals": [{key: value for key, value in s.items() if not key.startswith("_")} for s in signals],
            "_links": links,
        }

    # ── источники ──
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._trades_db}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _verdicts(self, start: date, end: date) -> list[dict]:
        """Последняя трасса ``filter.cluster`` на сигнал, МСК-дата бара в [start, end]."""
        lower = datetime.combine(start, datetime.min.time(), timezone.utc) - MSK
        upper = datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc) - MSK
        if not self._trades_db.is_file():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT signal_id, assignment_id, input_json, output_json, created_at FROM calculations "
                "WHERE algorithm=? ORDER BY created_at", (ALGORITHM,),
            ).fetchall()
            latest: dict[str, dict] = {}
            for row in rows:
                inputs = json.loads(row["input_json"])
                bar_close = _utc(_value(inputs, "bar_close"))
                if lower <= bar_close < upper:
                    latest[row["signal_id"]] = {"row": row, "inputs": inputs, "bar_close": bar_close,
                                                "output": json.loads(row["output_json"]).get("value") or {}}
            for item in latest.values():
                item["trade"] = self._trade(conn, item["row"]["signal_id"])
        return sorted(latest.values(), key=lambda item: (item["bar_close"], item["row"]["signal_id"]))

    @staticmethod
    def _trade(conn: sqlite3.Connection, signal_id: str) -> dict | None:
        trade = conn.execute("SELECT trade_id, phase FROM trades WHERE signal_id=? ORDER BY created_at LIMIT 1",
                             (signal_id,)).fetchone()
        if trade is None:
            return None
        filled = conn.execute("SELECT COUNT(*) FROM fills WHERE trade_id=?", (trade["trade_id"],)).fetchone()[0]
        if not filled:
            return None
        pnl = None
        if trade["phase"] == "CLOSED":
            position = conn.execute("SELECT net_realized_pnl FROM positions WHERE trade_id=?",
                                    (trade["trade_id"],)).fetchone()
            pnl = None if position is None else Decimal(str(position["net_realized_pnl"]))
        return {"trade_id": trade["trade_id"], "net_pnl": pnl}

    def _signal(self, item: dict, now: datetime, cache: dict[str, pd.DataFrame]) -> dict:
        inputs, output, bar_close = item["inputs"], item["output"], item["bar_close"]
        side = _value(inputs, "side")
        sign = 1 if side == "BUY" else -1
        uid = _value(inputs, "instrument_uid") or ""
        entry = _value(inputs, "close_at_entry")
        moves = {}
        for horizon in HORIZONS:
            moves[str(horizon)] = self._move(uid, entry, bar_close, horizon, sign, now, cache)
        trade = item["trade"]
        price = _value(inputs, "price")
        return {
            "signal_id": item["row"]["signal_id"],
            "bar_close": bar_close.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "instrument": _value(inputs, "instrument") or "",
            "assignment_id": item["row"]["assignment_id"] or "",
            "strategy": _value(inputs, "strategy") or "",
            "timeframe": _value(inputs, "timeframe") or "",
            "side": side,
            "price": str(price) if price is not None else "0",
            "verdict": output.get("verdict", UNKNOWN),
            "reason": output.get("reason", ""),
            "message": output.get("message", ""),
            "trade": None if trade is None else {
                "trade_id": trade["trade_id"],
                "net_pnl": None if trade["net_pnl"] is None else _money(trade["net_pnl"]),
            },
            "price_move": moves,
            "_mode": _value(inputs, "mode") or "off",
            "_closed_pnl": None if trade is None else trade["net_pnl"],
        }

    def _move(self, uid: str, entry, bar_close: datetime, horizon: int, sign: int, now: datetime,
              cache: dict[str, pd.DataFrame]) -> dict:
        target = bar_close + timedelta(minutes=horizon)
        if now < target:
            return {"status": "pending", "move_bps": None}
        if not uid or not isinstance(entry, dict) or not entry.get("close"):
            return {"status": "no_data", "move_bps": None}
        if uid not in cache:
            frame = self._candles(uid)
            cache[uid] = frame.sort_values("datetime").reset_index(drop=True) if not frame.empty else frame
        frame = cache[uid]
        if frame.empty:
            return {"status": "no_data", "move_bps": None}
        close_times = (frame["datetime"] + MINUTE).to_numpy(dtype="datetime64[ns]")
        bound = np.datetime64(target.replace(tzinfo=None))
        index = int(np.searchsorted(close_times, bound, side="right")) - 1
        if index < 0 or pd.Timestamp(close_times[index]) < pd.Timestamp(target.replace(tzinfo=None)) - STALE:
            return {"status": "no_data", "move_bps": None}
        move = sign * math.log(float(frame["close"].iloc[index]) / float(entry["close"])) * 1e4
        return {"status": "final", "move_bps": _decimal(move)}

    @staticmethod
    def _group(name: str, signals: list[dict]) -> dict:
        traded = [s for s in signals if s["trade"] is not None]
        closed = [s["_closed_pnl"] for s in traded if s["_closed_pnl"] is not None]
        price_move = {}
        for horizon in HORIZONS:
            values = [float(s["price_move"][str(horizon)]["move_bps"]) for s in signals
                      if s["price_move"][str(horizon)]["status"] == "final"]
            price_move[str(horizon)] = {
                "n": len(values),
                "mean_bps": _decimal(sum(values) / len(values)) if values else None,
                "median_bps": _decimal(median(values)) if values else None,
                "hit_rate": _decimal(sum(v > 0 for v in values) / len(values)) if values else None,
            }
        return {
            "verdict": name,
            "signals": len(signals),
            "trades": len(traded),
            "trades_net_pnl": _money(sum(closed, Decimal("0"))),
            "trades_won": sum(1 for pnl in closed if pnl > 0),
            "trades_lost": sum(1 for pnl in closed if pnl < 0),
            "price_move": price_move,
        }
