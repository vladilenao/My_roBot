import csv
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from src.config_loader import app_dir
from src.scheduler.timing import tf_period_minutes

UTC = timezone.utc

_DATE_TIME_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class JournalEvent:
    """Событие журнала, переданное репликой исполнителю.

    Соответствует строке журнала (все 23 колонки). Неизвестные колонки
    игнорируются при чтении файла, отсутствующие — пустые строки при записи.
    """

    id: int
    position_id: str
    status: str
    ts_order: str
    date: str
    side: str
    ts_entry: str
    deposit: str
    risk_pct: str
    risk_rub: str
    entry_price: str
    stop_price: str
    qty: str
    exit_price: str
    pnl_rub: str
    fee_rub: str
    price_step: str
    step_cost: str
    go_buy: str
    go_sell: str
    contract: str
    reason: str
    notes: str

    @property
    def ts_order_dt(self) -> datetime:
        return parse_dt(self.ts_order)

    @property
    def ts_entry_dt(self) -> Optional[datetime]:
        return parse_dt(self.ts_entry) if self.ts_entry else None

    @property
    def entry_price_f(self) -> Optional[float]:
        return _as_float(self.entry_price)

    @property
    def stop_price_f(self) -> Optional[float]:
        return _as_float(self.stop_price)

    @property
    def exit_price_f(self) -> Optional[float]:
        return _as_float(self.exit_price)

    @property
    def qty_i(self) -> int:
        return _as_int(self.qty)

    @property
    def pnl_f(self) -> Optional[float]:
        return _as_float(self.pnl_rub)

    @property
    def risk_pct_f(self) -> Optional[float]:
        return _as_float(self.risk_pct)

    @property
    def risk_rub_f(self) -> Optional[float]:
        return _as_float(self.risk_rub)


@dataclass(frozen=True)
class RestoredOrder:
    """Открытый ордер, восстановленный из журнала: entry/scale-in pending NEW-строка."""

    order_id: int
    position_id: str
    ticker: str
    side: str
    qty: int
    limit_price: float
    stop_price: float
    take_profit: Optional[float]
    timeframe: str
    ts_order: datetime
    risk_pct: Optional[float]
    risk_rub: Optional[float]
    ttl: Optional[int]  # None -> ордер живёт до ближайшего клиринга


@dataclass(frozen=True)
class RestoredPosition:
    """Открытая позиция, восстановленная из журнала growth-строками FILLED."""

    position_id: str
    ticker: str
    side: str
    qty: int
    avg_price: float
    stop_price: Optional[float]
    take_profit: Optional[float]
    ts_entry: Optional[datetime]
    over_risk: bool
    timeframe: str


@dataclass
class JournalState:
    """Состояние после replay журнала: позиции, отложенные ордера, счёт."""

    balance: float
    realized: float
    positions: dict[str, RestoredPosition] = field(default_factory=dict)
    orders: dict[int, RestoredOrder] = field(default_factory=dict)
    n_rows: int = 0


COLUMNS = [
    "id",
    "position_id",
    "status",
    "ts_order",
    "date",
    "side",
    "ts_entry",
    "deposit",
    "risk_pct",
    "risk_rub",
    "entry_price",
    "stop_price",
    "qty",
    "exit_price",
    "pnl_rub",
    "fee_rub",
    "price_step",
    "step_cost",
    "go_buy",
    "go_sell",
    "contract",
    "reason",
    "notes",
]

# Человекочитаемые заголовки CSV. Технические поля (id, position_id) и короткие
# устоявшиеся обозначения остаются латиницей, остальное — русскими названиями.
COLUMNS_RU = [
    "id",
    "position_id",
    "Статус",
    "Время заявки",
    "Дата",
    "Сторона",
    "Время входа",
    "Депозит",
    "Риск %",
    "Риск руб",
    "Цена входа",
    "Цена стопа",
    "Кол-во",
    "Цена выхода",
    "Прибыль руб",
    "Комиссия руб",
    "Шаг цены",
    "Стоимость шага",
    "ГО (покупка)",
    "ГО (продажа)",
    "Контракт",
    "Причина",
    "Заметки",
]

# техническое имя поля -> русский заголовок (для чтения и записи CSV)
_FIELD_TO_HEADER = dict(zip(COLUMNS, COLUMNS_RU))

_STATUSES = {"NEW", "FILLED", "CANCELLED", "EXPIRED", "CLEARING"}


def parse_dt(text: str) -> datetime:
    return datetime.strptime(text.strip(), _DATE_TIME_FMT).replace(tzinfo=UTC)


def format_dt(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.strftime(_DATE_TIME_FMT)


def parse_hhmm(text: str) -> timedelta:
    """Разбор времени клиринга `HH:MM` в timedelta от начала суток (UTC-совместимо)."""
    hh, mm = text.split(":")
    return timedelta(hours=int(hh), minutes=int(mm))


def make_position_id(ticker: str) -> str:
    return f"{ticker}-{uuid.uuid4().hex[:12]}"


def _as_float(text: str) -> Optional[float]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(text: str) -> int:
    text = (text or "").strip()
    return int(float(text)) if text else 0


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return repr(round(value, 6))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _extract_token(notes: str, key: str) -> Optional[str]:
    for part in (notes or "").split():
        if part.startswith(key + "="):
            return part[len(key) + 1:]
    return None


class TradeJournal:
    """Append-only выпуск дневника сделок в CSV (UTF-8-SIG).

    Журнал отвечает за хранение строк и консистентность обязательных полей.
    Решение о том, какая строка пишется и что она означает, остаётся за
    исполнителем (`JournalBroker`), который получает готовый `JournalEvent`.
    """

    def __init__(self, path: Path, next_event_id: int = 1):
        self.path = path
        self._next_event_id = next_event_id

    @property
    def next_id(self) -> int:
        return self._next_event_id

    @classmethod
    def from_config(cls, journal_file: str) -> "TradeJournal":
        return cls(Path(app_dir()) / journal_file)

    @staticmethod
    def ensure_exists(path: Path) -> bool:
        created = False
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerow(COLUMNS_RU)
            created = True
        return created

    @classmethod
    def created_on_init(cls, path: Path) -> "TradeJournal":
        created = cls.ensure_exists(path)
        return cls(path, next_event_id=cls._max_id(path) + 1 if not created else 1)

    @staticmethod
    def _max_id(path: Path) -> int:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = csv.DictReader(fh)
            return max(
                (_as_int(r.get("id")) for r in rows if r.get("id")),
                default=0,
            )

    def append(self, event: JournalEvent) -> None:
        if event.id < self._next_event_id:
            raise ValueError(
                f"id события журнала должен быть >= {self._next_event_id}, получено {event.id}"
            )
        if event.status not in _STATUSES:
            raise ValueError(f"Неизвестный статус события журнала: {event.status!r}")
        self._next_event_id = event.id + 1
        with self.path.open("a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS_RU, extrasaction="ignore")
            record = {
                _FIELD_TO_HEADER[col]: getattr(event, col, "")
                for col in COLUMNS
            }
            writer.writerow(record)

    def events(self) -> list[JournalEvent]:
        events: list[JournalEvent] = []
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                row = {
                    field: (raw.get(ru) or "")
                    for field, ru in zip(COLUMNS, COLUMNS_RU)
                }
                if not row["id"] or not row["status"]:
                    continue
                row["status"] = row["status"].upper()
                if row["status"] not in _STATUSES:
                    continue
                events.append(JournalEvent(**row))
        return events

    def replay(self, initial_deposit: float) -> JournalState:
        """Выполнение журнала в состояние системы: позиции, ордера, баланс."""
        return replay_events(self.events(), initial_deposit=initial_deposit)


def _position_ticker(position_id: str) -> str:
    return position_id.split("-", 1)[0] if "-" in position_id else position_id


def _parse_timeframe_from_notes(notes: str) -> str:
    return _extract_token(notes, "tf") or ""


def _parse_over_risk_from_notes(notes: str) -> bool:
    return _extract_token(notes, "over_risk") == "true"


def replay_events(events: Iterable[JournalEvent], initial_deposit: float = 0.0) -> JournalState:
    """Повтор журнала: восстанавливает счёт, позиции и отложенные entry-ордера.

    Счёт реплики берётся из последнего клирингового снимка (если он есть),
    иначе из `initial_deposit`. Реализованная прибыль накапливается по строкам
    закрытия; позиции и отложенные entry-ордера восстанавливаются по FILLED и
    NEW-строкам. Внешние данные (шаг цены, ГО) реплике не нужны — накладные
    расчёты делает `JournalBroker`.
    """
    state = JournalState(balance=initial_deposit, realized=0.0)
    pending: dict[int, RestoredOrder] = {}
    matched_orders: set[int] = set()
    positions: dict[str, list[dict]] = {}

    rows = list(events)
    clearing_deposits: list[float] = []
    for row in rows:
        if row.status == "CLEARING":
            dep = _as_float(row.deposit)
            if dep is not None:
                clearing_deposits.append(dep)
            continue
        if row.status not in ("NEW", "FILLED"):
            continue
        if row.status == "NEW":
            cls_ = row.side.upper()
            if cls_ not in ("BUY", "SELL") or row.qty_i <= 0 or row.entry_price_f is None:
                continue
            ttl = None
            tf = _parse_timeframe_from_notes(row.notes)
            if tf:
                minutes = tf_period_minutes(tf)
                ttl = 3600 if minutes <= 30 else (14400 if minutes < 240 else None)
            pending[row.id] = RestoredOrder(
                order_id=row.id,
                position_id=row.position_id,
                ticker=_position_ticker(row.position_id),
                side=cls_,
                qty=row.qty_i,
                limit_price=row.entry_price_f,
                stop_price=row.stop_price_f or 0.0,
                take_profit=_as_float(_extract_token(row.notes, "tp")),
                timeframe=tf,
                ts_order=row.ts_order_dt,
                risk_pct=row.risk_pct_f,
                risk_rub=row.risk_rub_f,
                ttl=ttl,
            )
            continue
        # FILLED-строка.
        match_id = next(
            (oid for oid, o in pending.items()
             if oid not in matched_orders
             and o.position_id == row.position_id
             and o.side == row.side.upper()
             and o.qty == row.qty_i),
            None,
        )
        if match_id is not None:
            matched_orders.add(match_id)
            positions.setdefault(row.position_id, []).append({
                "side": row.side.upper(),
                "qty": row.qty_i,
                "price": row.entry_price_f or 0.0,
                "ts_entry": row.ts_entry_dt,
                "notes": row.notes,
            })
            continue
        pnl = row.pnl_f
        if pnl is not None:
            state.realized += pnl
        position = positions.get(row.position_id)
        if position is not None:
            position.append({
                "side": "CLOSE",
                "qty": row.qty_i,
                "price": row.exit_price_f or 0.0,
                "ts_entry": None,
                "notes": row.notes,
            })

    state.orders = {oid: o for oid, o in pending.items() if oid not in matched_orders}

    for position_id, segments in positions.items():
        qty = 0
        weighted = 0.0
        side, avg_price = "", 0.0
        ts_entry, over_risk, tf = None, False, ""
        for seg in segments:
            if seg["side"] == "CLOSE":
                qty -= seg["qty"]
                continue
            if qty == 0:
                side = seg["side"]
                avg_price = 0.0
                weighted = 0.0
            weighted += seg["price"] * seg["qty"]
            qty += seg["qty"]
            avg_price = weighted / qty
            ts_entry = seg["ts_entry"] or ts_entry
            notes = seg["notes"]
            if _parse_over_risk_from_notes(notes):
                over_risk = True
            if not tf:
                tf = _parse_timeframe_from_notes(notes)
        if qty <= 0:
            continue
        state.positions[position_id] = RestoredPosition(
            position_id=position_id,
            ticker=_position_ticker(position_id),
            side=side,
            qty=qty,
            avg_price=round(avg_price, 6),
            stop_price=0.0,
            take_profit=None,
            ts_entry=ts_entry,
            over_risk=over_risk,
            timeframe=tf,
        )

    if clearing_deposits:
        state.balance = clearing_deposits[-1]
    state.n_rows = len(rows)
    return state