"""Основа разведки Финама: проверки с итогами, токен Ф0.1, отчёт (add-finam-probe, решения 5, 7, 8).

Итоги — как у разведки песочницы: «да», «нет», «не проверено», «ошибка».
«Нет» — ответ разведки, а не сбой. Каждое действие начинается с Ф0.1: JWT,
``TokenDetails`` и выбранный счёт среди счетов токена.
"""

from __future__ import annotations

import secrets
import string
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

from google.protobuf.timestamp_pb2 import Timestamp

from src.finam.gateway import FinamGateway
from src.finam.instruments import MOEX_MICS, FinamInstrument, Instruments, InstrumentNameError, dec
from src.finam.proto.google.type import interval_pb2
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2 as assets_pb2
from src.finam.proto.grpc.tradeapi.v1.auth import auth_service_pb2 as auth_pb2
from src.finam.proto.grpc.tradeapi.v1.marketdata import marketdata_service_pb2 as md_pb2
from src.finam.records import mask_account
from src.logging_setup import get_logger
from src.sandbox.gateway import BrokerCallError
from src.sandbox.probe import ERROR, NO, SKIPPED, YES, CheckResult

log = get_logger(__name__)

__all__ = ["ERROR", "NO", "SKIPPED", "YES", "Run", "Timing", "new_key", "num", "ts"]

KEY_PREFIX = "fp"
KEY_MAX = 20
_BASE36 = string.digits + string.ascii_lowercase
# Сессии расписания, в которые заявки разведки не ставятся (названия Финама неизвестны — по словам).
_CLOSED_WORDS = ("CLOSE", "BREAK", "MAINTENANCE", "HALT", "AUCTION")


def _base36(value: int) -> str:
    digits = ""
    while True:
        value, rest = divmod(value, 36)
        digits = _BASE36[rest] + digits
        if not value:
            return digits


def new_key(clock: Callable[[], datetime] | None = None) -> str:
    """``client_order_id`` разведки: ``fp`` + время в base36 + 6 случайных символов, не длиннее 20."""
    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    key = KEY_PREFIX + _base36(int(moment.timestamp() * 1000)) + "".join(secrets.choice(_BASE36) for _ in range(6))
    return key[:KEY_MAX]


def num(value: Decimal | None) -> str:
    if value is None:
        return "нет"
    text = f"{value.normalize():f}"
    return text


def ts(value: Timestamp | None) -> str:
    if value is None or (value.seconds == 0 and value.nanos == 0):
        return "нет"
    return value.ToDatetime(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def stamp(moment: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(moment.astimezone(timezone.utc))
    return result


def interval(start: datetime, end: datetime) -> interval_pb2.Interval:
    return interval_pb2.Interval(start_time=stamp(start), end_time=stamp(end))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass
class Timing:
    """Часы и паузы разведки; тесты подменяют их, чтобы не ждать минутами."""

    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    sleep: Callable[[float], None] = time.sleep
    poll_secs: float = 10
    event_wait_secs: float = 30
    stream_ready_secs: float = 3
    transactions_wait_secs: float = 60
    minute_secs: float = 60


@dataclass
class TokenInfo:
    account_ids: list = field(default_factory=list)
    readonly: bool = False
    expires_at: datetime | None = None
    created_at: datetime | None = None


class ProbeHalt(Exception):
    """Разведку надо остановить без уборки чужого: например, заявка неожиданно исполнилась."""


class Run:
    """Действие разведки: проверки по порядку, уборка в любом случае, отчёт."""

    TITLE = "Разведка Финама"
    CHECKS: tuple = ()
    PLACES_ORDERS = False

    def __init__(self, gateway: FinamGateway, *, account_id: str, demo: bool = False,
                 instruments: Instruments | None = None, timing: Timing | None = None,
                 params: str = "", out: Callable[[str], None] = print):
        self.gateway = gateway
        self.account_id = account_id
        self.demo = demo
        self.instruments = instruments
        self.timing = timing or Timing()
        self.params = params
        self.out = out
        checks = (("Ф0.1", "Токен и счёт"), *self.CHECKS)
        self.results = {code: CheckResult(code, title) for code, title in checks}
        self.started_at = self.timing.clock()
        self.finished_at: datetime | None = None
        self.exit_code = 0
        self.token = TokenInfo()
        self.notes: list[str] = []
        self.placed = False

    # --- итоги -----------------------------------------------------------
    def _set(self, code: str, outcome: str, detail: str = "") -> None:
        self.results[code].outcome = outcome
        self.results[code].detail = self.gateway.describe(detail)

    def _skip(self, codes, reason: str) -> None:
        for code in codes:
            if self.results[code].outcome == SKIPPED and not self.results[code].detail:
                self._set(code, SKIPPED, reason)

    def _rest(self, after: str) -> list[str]:
        codes = list(self.results)
        return codes[codes.index(after) + 1:]

    def _step(self, code: str, fn: Callable[[], bool | None]) -> bool:
        """Проверка: ``BrokerCallError`` и любой сбой → «ошибка», разведка продолжается."""
        try:
            return bool(fn())
        except ProbeHalt:
            raise
        except BrokerCallError as exc:
            self._set(code, ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001 — отчёт важнее падения
            log.error("Разведка Финама %s: сбой %s", code, type(exc).__name__)
            self._set(code, ERROR, f"сбой {type(exc).__name__}")
        return False

    # --- ход -------------------------------------------------------------
    def fail_auth(self, error: BrokerCallError) -> int:
        self._set("Ф0.1", ERROR, f"Auth: {error}")
        self._skip(self._rest("Ф0.1"), "нет JWT")
        self.exit_code = 1
        self.finished_at = self.timing.clock()
        return self.exit_code

    def run(self) -> int:
        try:
            if not self._step("Ф0.1", self._check_token):
                if self.exit_code == 0:
                    self.exit_code = 1
                self._skip(self._rest("Ф0.1"), "счёт не проверен")
                return self.exit_code
            if self.PLACES_ORDERS and self.token.readonly:
                self.exit_code = 2
                self._skip(self._rest("Ф0.1"), "токен только на чтение — заявки запрещены")
                return self.exit_code
            self._run()
        except ProbeHalt as exc:
            self.exit_code = 1
            self.notes.append(str(exc))
        except BrokerCallError as exc:
            self.exit_code = 1
            self.notes.append(f"разведка прервана: {exc}")
        except KeyboardInterrupt:
            self.exit_code = 1
            self.notes.append("прервано Ctrl+C")
        finally:
            if self.placed:
                try:
                    self.cleanup()
                except Exception as exc:  # noqa: BLE001 — уборка не должна терять отчёт
                    self.notes.append(f"уборка не завершена: {type(exc).__name__}")
            self.finished_at = self.timing.clock()
            self._skip(self.results, "не дошли")
        return self.exit_code

    def _run(self) -> None:
        raise NotImplementedError

    def cleanup(self) -> None:
        """Уборка после заявок разведки; у действий только на чтение её нет."""

    # --- Ф0.1 ------------------------------------------------------------
    def _check_token(self) -> bool:
        gateway = self.gateway
        details = gateway.call("auth.TokenDetails", auth_pb2.TokenDetailsRequest(token=gateway.jwt),
                               on_response=lambda reply: gateway.adopt_accounts(reply.account_ids))
        self.token = TokenInfo(
            account_ids=list(details.account_ids), readonly=details.readonly,
            expires_at=details.expires_at.ToDatetime(tzinfo=timezone.utc) if details.HasField("expires_at") else None,
            created_at=details.created_at.ToDatetime(tzinfo=timezone.utc) if details.HasField("created_at") else None,
        )
        accounts = ", ".join(mask_account(item) for item in details.account_ids) or "нет"
        life = ""
        if self.token.expires_at and self.token.created_at:
            minutes = (self.token.expires_at - self.token.created_at).total_seconds() / 60
            life = f", живёт {minutes:g} мин"
        # Площадок у токена десятки (прогон 23.11): в отчёте — Мосбиржа и число остальных, весь список — в записях.
        perms, others = [], 0
        for perm in details.md_permissions:
            where = perm.WhichOneof("condition") or "везде"
            place = getattr(perm, where) if where != "везде" else ""
            if place not in MOEX_MICS:
                others += 1
                continue
            level = perm.QuoteLevel.Name(perm.quote_level).removeprefix("QUOTE_LEVEL_")
            perms.append(f"{place}: {level}" + (f" с задержкой {perm.delay_minutes} мин"
                                                if perm.delay_minutes else " без задержки"))
        if others:
            perms.append(f"других площадок {others}")
        kind = "демо" if self.demo else "реальный"
        detail = (f"JWT получен (создан {ts(details.created_at)}, истекает {ts(details.expires_at)}{life}); "
                  f"readonly {'да' if details.readonly else 'нет'}; счета токена: {accounts}; "
                  f"рыночные данные: {'; '.join(perms) or 'нет'}")
        if self.account_id not in details.account_ids:
            self.exit_code = 1
            self._set("Ф0.1", NO, f"{kind} счёт {mask_account(self.account_id)} не найден среди счетов токена. {detail}")
            return False
        self._set("Ф0.1", YES, f"{kind} счёт {mask_account(self.account_id)} есть в токене. {detail}")
        return True

    # --- общее для заявок ------------------------------------------------
    def resolve(self, name: str, code: str) -> FinamInstrument | None:
        try:
            item = self.instruments.resolve(name)
        except InstrumentNameError as exc:
            self.exit_code = 2
            self._set(code, ERROR, str(exc))
            return None
        if not item.found:
            self._set(code, NO, f"{item.name}: символ в Assets не найден")
            return None
        item.asset = self.gateway.call("assets.GetAsset", assets_pb2.GetAssetRequest(
            symbol=item.symbol, account_id=self.account_id))
        item.params = self.gateway.call("assets.GetAssetParams", assets_pb2.GetAssetParamsRequest(
            symbol=item.symbol, account_id=self.account_id))
        return item

    def market_state(self, item: FinamInstrument) -> tuple[bool, Decimal | None, str]:
        """Идут ли торги сейчас и последняя цена Финама; подробность для отчёта."""
        quote = self.gateway.call("marketdata.LastQuote", md_pb2.QuoteRequest(symbol=item.symbol)).quote
        last = dec(quote.last) if quote.HasField("last") else None
        now = self.timing.clock()
        schedule = self.gateway.call("assets.Schedule", assets_pb2.ScheduleRequest(symbol=item.symbol))
        current = None
        for session in schedule.sessions:
            start = session.interval.start_time.ToDatetime(tzinfo=timezone.utc)
            end = session.interval.end_time.ToDatetime(tzinfo=timezone.utc)
            if start <= now < end:
                current = session.type
                break
        tradable = item.params.is_tradable.value if item.params is not None and item.params.HasField(
            "is_tradable") else True
        open_now = (current is not None and not any(word in current.upper() for word in _CLOSED_WORDS)
                    and tradable and last is not None)
        detail = (f"сессия сейчас: {current or 'нет'}, торгуется: {'да' if tradable else 'нет'}, "
                  f"последняя цена {num(last)} ({ts(quote.timestamp)})")
        return open_now, last, detail

    def step_of(self, item: FinamInstrument) -> Decimal:
        step = item.price_step or (item.tinvest.price_step if item.tinvest else None)
        if not step:
            raise ProbeHalt(f"{item.name}: шаг цены неизвестен — заявки не ставятся")
        return step

    def quantity_of(self, item: FinamInstrument) -> Decimal:
        """Один лот: у фьючерса 1, у акции — ``lot_size`` Финама в штуках."""
        if item.kind == "share":
            return item.lot_size or Decimal(item.tinvest.lot if item.tinvest else 1)
        return Decimal(1)

    def since(self, minutes: float = 1) -> datetime:
        return self.started_at - timedelta(minutes=minutes)

    # --- отчёт -----------------------------------------------------------
    def report_lines(self) -> list[str]:
        finished = self.finished_at or self.timing.clock()
        kind = "демо-счёт" if self.demo else "реальный счёт"
        lines = [
            f"# {self.TITLE} ({kind} {mask_account(self.account_id)})",
            "",
            f"Начало {self.started_at:%Y-%m-%d %H:%M:%S} UTC, конец {finished:%Y-%m-%d %H:%M:%S} UTC.",
            f"Параметры: {self.params or 'нет'}.",
            "",
            "| № | Проверка | Итог | Подробность |",
            "|---|---|---|---|",
        ]
        for result in self.results.values():
            detail = result.detail.replace("|", "/").replace("\n", " ")
            lines.append(f"| {result.code} | {result.title} | {result.outcome} | {detail} |")
        if self.notes:
            lines.append("")
            lines.extend(f"- {self.gateway.describe(note)}" for note in self.notes)
        return lines

    def write_report(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "report.md"
        path.write_text("\n".join(self.report_lines()) + "\n", encoding="utf-8")
        return path
