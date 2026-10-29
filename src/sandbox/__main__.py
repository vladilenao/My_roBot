"""``python -m src.sandbox <действие>`` — команда песочницы Т-Инвест (add-sandbox-probe).

Коды выхода: 0 — действие выполнено, 1 — не выполнено из-за ответа брокера
или сети, 2 — ошибка запуска (нет токена, неверные параметры, имя инструмента).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from t_tech.invest.schemas import OrderDirection

from src.config import (
    CLOUDFLARE_URL,
    MARKET_DATA_EXPORT_TOKEN,
    MARKET_STRUCTURE_REDIS_PASSWORD,
    ROBOT_CONTROL_TOKEN,
    TELEGRAM_BOT_TOKEN,
    TINKOFF_SANDBOX_TOKEN,
    TINKOFF_TOKEN,
    runtime_dir,
)
from src.logging_setup import get_logger, setup_logging
from src.sandbox.account import AccountConflict, robot_account
from src.sandbox.actions import manual_order, pay_in, reset, show_account
from src.sandbox.gateway import BrokerCallError, SandboxGateway
from src.sandbox.instruments import InstrumentNameError, resolve_instrument
from src.sandbox.probe import DEFAULT_INSTRUMENT, Probe, ProbeSettings, ProbeTiming
from src.sandbox.records import CallRecorder

log = get_logger(__name__)

ACTIONS = ("account", "pay-in", "buy", "sell", "reset", "probe")
USAGE = """Команда песочницы Т-Инвест: python -m src.sandbox <действие>

  account                 состояние счёта робота в песочнице
  pay-in <рубли>          пополнить счёт песочницы
  buy <инструмент> <лоты> купить по рынку
  sell <инструмент> <лоты> продать по рынку
  reset                   снять все заявки и стоп-заявки, закрыть позиции
  probe [инструмент] [--stop-ticks N] [--stop-wait МИН] [--min-rub СУММА]
                          разведка возможностей песочницы (по умолчанию NG)"""
NO_TOKEN = "Не задан TINKOFF_SANDBOX_TOKEN"
_SECRETS = (TINKOFF_SANDBOX_TOKEN, TINKOFF_TOKEN, TELEGRAM_BOT_TOKEN, CLOUDFLARE_URL,
            MARKET_DATA_EXPORT_TOKEN, ROBOT_CONTROL_TOKEN, MARKET_STRUCTURE_REDIS_PASSWORD)


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse печатает в stderr и выходит; нам нужен свой текст и код 2
        raise _UsageError(message)


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужно целое число, получено {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"нужно положительное число, получено {text}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m src.sandbox", add_help=False)
    actions = parser.add_subparsers(dest="action")
    actions.add_parser("account", add_help=False)
    actions.add_parser("reset", add_help=False)
    pay = actions.add_parser("pay-in", add_help=False)
    pay.add_argument("amount", type=_positive)
    for name in ("buy", "sell"):
        order = actions.add_parser(name, add_help=False)
        order.add_argument("instrument")
        order.add_argument("lots", type=_positive)
    probe = actions.add_parser("probe", add_help=False)
    probe.add_argument("instrument", nargs="?", default=DEFAULT_INSTRUMENT)
    probe.add_argument("--stop-ticks", type=_positive, default=2)
    probe.add_argument("--stop-wait", type=float, default=10)
    probe.add_argument("--min-rub", type=_positive, default=1_000_000)
    for sub in actions.choices.values():
        sub.error = parser.error
    return parser


def _setup_logging(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    setup_logging(service_uid="sandbox", log_file=str(directory / "sandbox.log"), level="DEBUG",
                  secrets=_SECRETS)


def main(argv: Sequence[str] | None = None, *, token: str | None = TINKOFF_SANDBOX_TOKEN,
         gateway_factory: Callable[..., SandboxGateway] = SandboxGateway,
         out: Callable[[str], None] = print, base_dir: Path | None = None,
         timing: ProbeTiming | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ACTIONS:
        out(USAGE)
        return 2
    try:
        args = _parser().parse_args(argv)
    except _UsageError as exc:
        out(f"Ошибка параметров: {exc}")
        out(USAGE)
        return 2
    if args.action == "probe" and args.stop_wait <= 0:
        out("Ошибка параметров: --stop-wait должен быть больше нуля")
        return 2
    if not token:
        out(NO_TOKEN)
        return 2
    directory = (base_dir or runtime_dir()) / "sandbox"
    _setup_logging(directory)
    log.info("Команда песочницы: %s", args.action)
    if args.action == "probe":
        return _probe(args, token, gateway_factory, out, directory, timing)
    try:
        with gateway_factory(token) as gateway:
            account = robot_account(gateway)
            if account.opened:
                out(f"Открыт счёт песочницы {account.masked}")
            if args.action == "account":
                show_account(gateway, account, out)
            elif args.action == "pay-in":
                pay_in(gateway, account, args.amount, out)
            elif args.action == "reset":
                return 0 if reset(gateway, account, out) else 1
            else:
                instrument = resolve_instrument(gateway, args.instrument)
                if instrument.chosen:
                    out(instrument.chosen)
                direction = (OrderDirection.ORDER_DIRECTION_BUY if args.action == "buy"
                             else OrderDirection.ORDER_DIRECTION_SELL)
                manual_order(gateway, account, instrument, args.lots, direction, out)
    except InstrumentNameError as exc:
        out(str(exc))
        return 2
    except (AccountConflict, BrokerCallError) as exc:
        out(f"Не выполнено: {exc}")
        return 1
    return 0


def _probe(args, token, gateway_factory, out, directory: Path, timing: ProbeTiming | None) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = directory / f"probe-{stamp}"
    recorder = CallRecorder(run_dir / "responses.jsonl")
    settings = ProbeSettings(instrument=args.instrument, stop_ticks=args.stop_ticks,
                             stop_wait_min=args.stop_wait, min_rub=args.min_rub)
    out(f"Разведка песочницы: {args.instrument}. Ожидание стопа до {args.stop_wait:g} мин.")
    probe = None
    code = 1
    try:
        with gateway_factory(token, recorder=recorder) as gateway:
            probe = Probe(gateway, settings, timing=timing)
            code = probe.run()
    except BrokerCallError as exc:
        out(f"Разведка прервана: {exc}")
    finally:
        if probe is not None:
            probe.write_report(run_dir)
            for line in probe.report_lines():
                out(line)
        out(f"Отчёт и записи: {run_dir}")
    return code


if __name__ == "__main__":
    sys.exit(main())
