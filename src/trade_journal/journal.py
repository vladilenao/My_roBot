import csv
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from src.config_loader import app_dir
from src.logging_setup import get_logger
from src.scheduler.timing import tf_period_minutes

UTC = timezone.utc
_MSK = timezone(timedelta(hours=3))

_DATE_TIME_FMT = "%Y-%m-%d %H:%M:%S"

log = get_logger(__name__)


class OpType(str, Enum):
    """Тип операции в ленте журнала (колонка «операция»)."""

    ORDER = "ЗАЯВКА"      # принята, ещё не исполнена
    ENTRY = "ВХОД"        # исполненная входная сделка
    ADD = "ДОБОР"         # добор (scale-in) — увеличивает количество
    TAKE = "ТЕЙК"         # частичный тейк — уменьшает количество
    EXIT = "ВЫХОД"        # полный выход — закрывает остаток
    SNAPSHOT = "СНИМОК"   # клиринговый снимок (баланс, реализация, число позиций)
    CANCEL = "ОТМЕНА"     # отменённая/истёкшая заявка или отклонённый вход


_OP_TYPES = set(OpType)

POSITION_STATUS_OPEN = "ОТКРЫТА"
POSITION_STATUS_CLOSED = "ЗАКРЫТА"
POSITION_STATUS_CANCELLED = "ОТМЕНЕНА"


@dataclass(frozen=True)
class JournalEvent:
    """Одна запись ленты журнала.

    Соответствует строке `journal.csv` (18 колонок). Неизвестные колонки
    игнорируются при чтении файла, отсутствующие — пустые строки при записи.
    Времена в файле — МСК, контракт — короткое имя (`NG-10.26`).
    """

    id: int
    op: str
    ts: str
    order_id: str
    position_id: str
    contract: str
    side: str
    qty: str
    price: str
    stop: str
    pnl_part: str
    fee: str
    deposit: str
    risk_pct: str
    risk_rub: str
    go: str
    reason: str
    notes: str

    @property
    def op_type(self) -> OpType:
        return OpType(self.op.upper())

    @property
    def ts_dt(self) -> datetime:
        return parse_dt(self.ts)

    @property
    def order_id_i(self) -> int:
        return _as_int(self.order_id)

    @property
    def price_f(self) -> Optional[float]:
        return _as_float(self.price)

    @property
    def stop_f(self) -> Optional[float]:
        return _as_float(self.stop)

    @property
    def pnl_f(self) -> Optional[float]:
        return _as_float(self.pnl_part)

    @property
    def fee_f(self) -> Optional[float]:
        return _as_float(self.fee)

    @property
    def deposit_f(self) -> Optional[float]:
        return _as_float(self.deposit)

    @property
    def risk_pct_f(self) -> Optional[float]:
        return _as_float(self.risk_pct)

    @property
    def risk_rub_f(self) -> Optional[float]:
        return _as_float(self.risk_rub)

    @property
    def go_f(self) -> Optional[float]:
        return _as_float(self.go)

    @property
    def qty_i(self) -> int:
        return _as_int(self.qty)


@dataclass(frozen=True)
class RestoredOrder:
    """Отложенный ордер, восстановленный из журнала: неисполненная ЗАЯВКА."""

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
    """Открытая позиция, восстановленная из журнала записями ВХОД/ДОБОР."""

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


# Колонки ленты `journal.csv` (внутренние имена полей).
COLUMNS = [
    "id",
    "op",
    "ts",
    "order_id",
    "position_id",
    "contract",
    "side",
    "qty",
    "price",
    "stop",
    "pnl_part",
    "fee",
    "deposit",
    "risk_pct",
    "risk_rub",
    "go",
    "reason",
    "notes",
]

# Человекочитаемые заголовки CSV. Технические поля (id, ts, order_id,
# position_id, qty) устоявшиеся — латиницей, остальное — русскими названиями.
COLUMNS_RU = [
    "id",
    "операция",
    "ts",
    "order_id",
    "position_id",
    "контракт",
    "сторона",
    "qty",
    "цена",
    "стоп",
    "pnl_часть",
    "комиссия",
    "депозит",
    "риск_%",
    "риск_руб",
    "го",
    "причина",
    "заметки",
]

# Колонки карточек `positions.csv` (одна строка — жизнь позиции).
POSITIONS_COLUMNS = [
    "position_id",
    "contract",
    "side",
    "ts_entry",
    "qty",
    "avg_price",
    "stop",
    "ts_exit",
    "exit_price",
    "pnl",
    "fee",
    "risk_pct",
    "risk_rub",
    "go_max",
    "status",
    "exit_reason",
]

POSITIONS_COLUMNS_RU = [
    "position_id",
    "контракт",
    "направление",
    "ts_входа",
    "qty_итог",
    "цена_средняя",
    "стоп",
    "ts_выхода",
    "цена_выхода_средняя",
    "pnl_итог",
    "комиссия_итог",
    "риск_%",
    "риск_руб",
    "го_макс",
    "статус",
    "причина_выхода",
]

# техническое имя поля -> русский заголовок (для чтения и записи CSV)
_FIELD_TO_HEADER = dict(zip(COLUMNS, COLUMNS_RU))
_POSITIONS_FIELD_TO_HEADER = dict(zip(POSITIONS_COLUMNS, POSITIONS_COLUMNS_RU))


def parse_dt(text: str) -> datetime:
    """МСК-метка из файла -> осознанный UTC (`datetime`).

    Файлы журнала хранят пользовательские времена в МСК; внутри движок
    продолжает считать в UTC, поэтому на границе разбора метка конвертируется.
    """
    msk = datetime.strptime(text.strip(), _DATE_TIME_FMT).replace(tzinfo=_MSK)
    return msk.astimezone(UTC)


def format_dt(dt: datetime) -> str:
    """UTC-`datetime` -> МСК-метка для записи в файл журнала."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_MSK).strftime(_DATE_TIME_FMT)


def parse_hhmm(text: str) -> timedelta:
    """Разбор времени клиринга `HH:MM` в timedelta от начала суток (МСК-совместимо)."""
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


def _parse_timeframe_from_notes(notes: str) -> str:
    return _extract_token(notes, "tf") or ""


def _parse_over_risk_from_notes(notes: str) -> bool:
    return _extract_token(notes, "over_risk") == "true"


def _ttl_for(timeframe: str) -> Optional[int]:
    if not timeframe:
        return None
    minutes = tf_period_minutes(timeframe)
    if minutes <= 30:
        return 3600
    if minutes < 240:
        return 4 * 3600
    return None  # живёт до ближайшего клиринга


def _position_ticker(position_id: str) -> str:
    return position_id.split("-", 1)[0] if "-" in position_id else position_id


def _default_positions_path(path: Path) -> Path:
    """Имя карточек по умолчанию: суффикс `_positions` перед расширением."""
    return path.with_name(f"{path.stem}_positions{path.suffix}")


@dataclass
class PositionCard:
    """Производная карточка позиции: одна строка `positions.csv`.

    Всегда производная от ленты (никогда не источник истины). Поля, не
    применимые к статусу позиции, могут быть пустыми; системно-пустых колонок
    в карточке быть не должно.
    """

    position_id: str
    contract: str = ""
    side: str = ""
    ts_entry: str = ""
    qty: int = 0
    avg_price: float = 0.0
    stop: str = ""
    ts_exit: str = ""
    exit_price: Optional[float] = None
    pnl: float = 0.0
    fee: float = 0.0
    risk_pct: str = ""
    risk_rub: str = ""
    go_max: float = 0.0
    status: str = POSITION_STATUS_OPEN
    exit_reason: str = ""
    # внутренние аккумуляторы для инкрементального пересчёта
    opened: bool = False
    _weighted: float = 0.0
    _closed_qty: int = 0
    _exit_value: float = 0.0

    def to_row(self) -> dict[str, str]:
        opened = self.opened
        return {
            "position_id": self.position_id,
            "contract": self.contract,
            "side": self.side,
            "ts_entry": self.ts_entry,
            "qty": str(self.qty),
            "avg_price": _fmt(self.avg_price) if opened else "",
            "stop": self.stop,
            "ts_exit": self.ts_exit,
            "exit_price": _fmt(self.exit_price) if self.exit_price is not None else "",
            "pnl": _fmt(self.pnl),
            "fee": _fmt(self.fee),
            "risk_pct": self.risk_pct,
            "risk_rub": self.risk_rub,
            "go_max": _fmt(self.go_max),
            "status": self.status,
            "exit_reason": self.exit_reason,
        }


def _apply_event_to_cards(
    cards: dict[str, PositionCard],
    seen_orders: set[str],
    event: JournalEvent,
) -> None:
    """Инкрементальное обновление карточек одной записью ленты."""
    pid = event.position_id
    op = event.op_type
    if op is OpType.ORDER:
        seen_orders.add(pid)
        return
    if op in (OpType.ENTRY, OpType.ADD):
        card = cards.get(pid) or PositionCard(position_id=pid)
        cards[pid] = card
        card.opened = True
        card.contract = event.contract or card.contract
        card.side = event.side.upper() or card.side
        qty = event.qty_i
        price = event.price_f or 0.0
        card._weighted += price * qty
        card.qty += qty
        card.avg_price = round(card._weighted / card.qty, 6) if card.qty else 0.0
        card.stop = event.stop or card.stop
        card.risk_pct = card.risk_pct or event.risk_pct
        card.risk_rub = card.risk_rub or event.risk_rub
        go = event.go_f or 0.0
        card.go_max = max(card.go_max, go)
        if not card.ts_entry:
            card.ts_entry = event.ts
        card.status = POSITION_STATUS_OPEN
        return
    if op in (OpType.TAKE, OpType.EXIT):
        card = cards.get(pid)
        if card is None:
            return
        qty = event.qty_i
        card.pnl += event.pnl_f or 0.0
        card.fee += event.fee_f or 0.0
        exit_price = event.price_f or 0.0
        card.qty = max(0, card.qty - qty)
        card._closed_qty += qty
        card._exit_value += exit_price * qty
        if card.qty == 0:
            card.status = POSITION_STATUS_CLOSED
            card.ts_exit = event.ts
            card.exit_reason = event.reason or card.exit_reason
            if card._closed_qty:
                card.exit_price = round(card._exit_value / card._closed_qty, 6)
        return
    if op is OpType.SNAPSHOT:
        return
    if op is OpType.CANCEL:
        if pid not in cards and pid not in seen_orders:
            return  # отклонённая заявка без жизни позиции (карточки не создаём)
        card = cards.get(pid) or PositionCard(position_id=pid)
        cards[pid] = card
        if not card.opened:
            card.status = POSITION_STATUS_CANCELLED
        return


class TradeJournal:
    """Append-only выпуск дневника сделок в CSV (UTF-8-SIG).

    Два файла: `journal.csv` — append-only лента (источник истины), и
    `positions.csv` — производные карточки позиций. Карточки поддерживаются
    инкрементально в памяти на каждую append-запись и каждый раз переписываются
    снапшотом из памяти, поэтому `positions.csv` всегда актуальны ленте. При
    старте с существующей лентой карточки пересчитываются полным реплеем.
    """

    def __init__(
        self,
        path: Path,
        next_event_id: int = 1,
        positions_path: Optional[Path] = None,
    ):
        self.path = path
        self._next_event_id = next_event_id
        self._positions_path = positions_path or _default_positions_path(path)
        self._cards: dict[str, PositionCard] = {}
        self._seen_orders: set[str] = set()

    @property
    def next_id(self) -> int:
        return self._next_event_id

    @property
    def positions_path(self) -> Path:
        return self._positions_path

    @classmethod
    def from_config(
        cls,
        journal_file: str,
        positions_file: Optional[str] = None,
    ) -> "TradeJournal":
        positions_path = (
            Path(app_dir()) / positions_file if positions_file else None
        )
        return cls(Path(app_dir()) / journal_file, positions_path=positions_path)

    @staticmethod
    def _header_row() -> list[str]:
        return list(COLUMNS_RU)

    @classmethod
    def ensure_exists(cls, path: Path) -> bool:
        """Создаёт файл при отсутствии; регенерирует заголовок как истину.

        Возвращает True, если файл был создан или его содержимое сброшено
        (заголовок не соответствует схеме). Серьёзная миграция легаси-файлов
        выполняется отдельно (`create_journal_broker`), здесь — только проверка
        целостности схемы заголовка.
        """
        created = False
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerow(COLUMNS_RU)
            return True
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                header = []
        if header != COLUMNS_RU:
            log.warning(
                "Заголовок %s не соответствует схеме журнала — файл обнуляется",
                path,
            )
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerow(COLUMNS_RU)
            created = True
        return created

    @classmethod
    def created_on_init(
        cls,
        path: Path,
        positions_path: Optional[Path] = None,
    ) -> "TradeJournal":
        created = cls.ensure_exists(path)
        journal = cls(
            path,
            next_event_id=cls._max_id(path) + 1 if not created else 1,
            positions_path=positions_path,
        )
        if created:
            journal.write_cards()
        else:
            journal.rebuild_cards()
        return journal

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
        if event.op.upper() not in _OP_TYPES:
            raise ValueError(f"Неизвестный тип операции журнала: {event.op!r}")
        self._next_event_id = event.id + 1
        with self.path.open("a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS_RU, extrasaction="ignore")
            record = {
                _FIELD_TO_HEADER[col]: getattr(event, col, "")
                for col in COLUMNS
            }
            writer.writerow(record)
        _apply_event_to_cards(self._cards, self._seen_orders, event)
        self.write_cards()

    def write_cards(self) -> None:
        """Перезапись `positions.csv` снапшотом из памяти (производный файл)."""
        self._positions_path.parent.mkdir(parents=True, exist_ok=True)
        with self._positions_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=POSITIONS_COLUMNS_RU, extrasaction="ignore"
            )
            writer.writeheader()
            for card in self._cards.values():
                writer.writerow(
                    {
                        _POSITIONS_FIELD_TO_HEADER[k]: v
                        for k, v in card.to_row().items()
                    }
                )

    def cards(self) -> list[PositionCard]:
        """Карточки позиций (статус/состав соответствует текущей ленте)."""
        return list(self._cards.values())

    def rebuild_cards(self) -> None:
        """Полный пересчёт карточек из ленты (истина — только journal.csv)."""
        self._cards = {}
        self._seen_orders = set()
        for event in self.events():
            _apply_event_to_cards(self._cards, self._seen_orders, event)
        self.write_cards()

    def events(self) -> list[JournalEvent]:
        events: list[JournalEvent] = []
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for number, raw in enumerate(reader, start=2):
                row = {
                    field: (raw.get(ru) or "")
                    for field, ru in zip(COLUMNS, COLUMNS_RU)
                }
                if not row["id"] or not row["op"]:
                    continue
                row["op"] = row["op"].upper()
                if row["op"] not in _OP_TYPES:
                    log.warning(
                        "journal.csv: строка %d с неизвестной операцией %r отброшена",
                        number,
                        row["op"],
                    )
                    continue
                events.append(JournalEvent(**row))
        return events

    def replay(self, initial_deposit: float) -> JournalState:
        """Выполнение ленты в состояние системы: позиции, ордера, баланс."""
        return replay_events(self.events(), initial_deposit=initial_deposit)


def replay_events(
    events: Iterable[JournalEvent],
    initial_deposit: float = 0.0,
) -> JournalState:
    """Повтор ленты: восстанавливает счёт, позиции и отложенные заявки.

    Баланс — из последнего СНИМОК (иначе стартовый депозит). Открытые позиции
    агрегируют ВХОД/ДОБОР (средневзвешенная цена), остаток уменьшается ТЕЙК/
    ВЫХОД; реализованная прибыль накапливается по закрывающим записям.
    Отложенные заявки — ЗАЯВКА без последующего ВХОД/ДОБОР (и без ОТМЕНА).
    Внешние данные (шаг цены, ГО) реплике не нужны — их рассчитывает брокер.
    """
    state = JournalState(balance=initial_deposit, realized=0.0)
    rows = list(events)
    deposits: list[float] = []
    all_orders: dict[int, RestoredOrder] = {}
    executed_order_ids: set[int] = set()
    terminal_order_ids: set[int] = set()
    agg: dict[str, dict[str, Any]] = {}

    for row in events:
        op = row.op_type
        if op is OpType.SNAPSHOT:
            dep = row.deposit_f
            if dep is not None:
                deposits.append(dep)
            continue
        if op is OpType.CANCEL:
            if row.order_id_i:
                terminal_order_ids.add(row.order_id_i)
            continue
        if op is OpType.ORDER:
            cls_ = row.side.upper()
            if cls_ not in ("BUY", "SELL") or row.qty_i <= 0 or row.price_f is None:
                continue
            tf = _parse_timeframe_from_notes(row.notes)
            order_id = row.order_id_i or row.id
            all_orders[order_id] = RestoredOrder(
                order_id=order_id,
                position_id=row.position_id,
                ticker=_position_ticker(row.position_id),
                side=cls_,
                qty=row.qty_i,
                limit_price=row.price_f,
                stop_price=row.stop_f or 0.0,
                take_profit=_as_float(_extract_token(row.notes, "tp")),
                timeframe=tf,
                ts_order=row.ts_dt,
                risk_pct=row.risk_pct_f,
                risk_rub=row.risk_rub_f,
                ttl=_ttl_for(tf),
            )
            continue
        if op in (OpType.ENTRY, OpType.ADD):
            if row.order_id_i:
                executed_order_ids.add(row.order_id_i)
            pid = row.position_id
            seg = agg.setdefault(
                pid,
                {
                    "side": row.side.upper(),
                    "qty": 0,
                    "weighted": 0.0,
                    "ts_entry": None,
                    "over_risk": False,
                    "tf": "",
                    "stop": None,
                },
            )
            seg["qty"] += row.qty_i
            seg["weighted"] += (row.price_f or 0.0) * row.qty_i
            if row.ts:
                seg["ts_entry"] = row.ts_dt
            if _parse_over_risk_from_notes(row.notes):
                seg["over_risk"] = True
            if not seg["tf"]:
                seg["tf"] = _parse_timeframe_from_notes(row.notes)
            if row.stop_f:
                seg["stop"] = row.stop_f
            continue
        if op in (OpType.TAKE, OpType.EXIT):
            pnl = row.pnl_f
            if pnl is not None:
                state.realized += pnl
            seg = agg.get(row.position_id)
            if seg is not None:
                seg["qty"] = max(0, seg["qty"] - row.qty_i)

    state.orders = {
        oid: o
        for oid, o in all_orders.items()
        if oid not in executed_order_ids and oid not in terminal_order_ids
    }

    for position_id, seg in agg.items():
        if seg["qty"] <= 0:
            continue
        avg_price = round(seg["weighted"] / seg["qty"], 6)
        state.positions[position_id] = RestoredPosition(
            position_id=position_id,
            ticker=_position_ticker(position_id),
            side=seg["side"],
            qty=seg["qty"],
            avg_price=avg_price,
            stop_price=seg["stop"],
            take_profit=None,
            ts_entry=seg["ts_entry"],
            over_risk=seg["over_risk"],
            timeframe=seg["tf"],
        )

    if deposits:
        state.balance = deposits[-1]
    state.n_rows = len(rows)
    return state