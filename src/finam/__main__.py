"""``python -m src.finam <действие>`` — разведка Finam Trade API (add-finam-probe).

Коды выхода: 0 — действие выполнено (у разведки — дошла до отчёта), 1 — не
выполнено из-за ответа брокера или сети, 2 — ошибка запуска (нет секрета или
номера счёта, неверные параметры, неизвестное имя, нет подтверждения).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src import config
from src.finam.account import DEFAULT_INSTRUMENTS, AccountRun
from src.finam.checks import Timing
from src.finam.gateway import FinamGateway
from src.finam.instruments import Instruments, ReferenceGateway
from src.finam.orders import cancel_by_number, cleanup_orders
from src.finam.probe import DEFAULT_INSTRUMENT, LimitProbe
from src.finam.probe_jwt import JwtProbe, LimitsProbe
from src.finam.probe_stops import StopProbe, TriggerProbe
from src.finam.records import CallRecorder, mask_account
from src.logging_setup import get_logger, setup_logging
from src.sandbox.gateway import BrokerCallError

log = get_logger(__name__)

ACTIONS = ("account", "probe", "probe-stops", "probe-trigger", "probe-jwt", "probe-limits", "cleanup")
ORDER_ACTIONS = ("probe", "probe-stops", "probe-trigger", "cleanup")
USAGE = """Разведка Finam Trade API: python -m src.finam <действие> [--demo]

Только чтение:
  account [инструменты…] [--days N]
                          токен, счёт, заявки, сделки, транзакции, инструменты,
                          цены, расписание, лимиты (по умолчанию Si BR NG GOLD SBER)
  probe-jwt [--minutes N] срок жизни JWT и стримов (по умолчанию 20 мин)
  probe-limits [--calls N] ответ при превышении лимита запросов (по умолчанию 250)

С заявками (далеко от рынка, не исполняются):
  probe [инструмент] [--offset-pct P]
                          лимитная заявка 1 лот на P% ниже рынка (NG, 3%)
  probe-stops [инструмент] [--offset-pct P] [--side buy|sell] [--keep] [--tif unset|day|gtc]
                          стоп-заявка 1 лот в P% от рынка (NG, 10%, покупка);
                          --tif — time_in_force стопа (по умолчанию не задаётся)
  cleanup [--order N]     снять заявки разведки (ключ fp…), чужие не трогать;
                          --order — снять одну заявку по номеру Финама

Со сделкой (только по подтверждению):
  probe-trigger [инструмент] [--ticks N] [--side buy|sell] [--wait МИН] [--real]
                          стоп в N шагах от рынка, сделка 1 лот и её закрытие

--demo — демо-счёт (FINAM_DEMO_ACCOUNT_ID). Без --demo probe-trigger требует --real."""
CONFIRM_WORD = "ДА"


@dataclass
class FinamEnv:
    secret: str | None = config.FINAM_SECRET_TOKEN
    account_id: str | None = config.FINAM_ACCOUNT_ID
    demo_secret: str | None = config.FINAM_DEMO_SECRET_TOKEN
    demo_account_id: str | None = config.FINAM_DEMO_ACCOUNT_ID
    tinkoff_token: str | None = config.TINKOFF_TOKEN

    def pick(self, demo: bool) -> tuple[str | None, str | None, str, str]:
        """Секрет, номер счёта и имена их переменных для выбранного счёта."""
        if demo:
            if self.demo_secret:
                return self.demo_secret, self.demo_account_id, "FINAM_DEMO_SECRET_TOKEN", "FINAM_DEMO_ACCOUNT_ID"
            return self.secret, self.demo_account_id, "FINAM_SECRET_TOKEN", "FINAM_DEMO_ACCOUNT_ID"
        return self.secret, self.account_id, "FINAM_SECRET_TOKEN", "FINAM_ACCOUNT_ID"


def _secrets(env: FinamEnv) -> tuple:
    return (env.secret, env.account_id, env.demo_secret, env.demo_account_id, env.tinkoff_token,
            config.TINKOFF_SANDBOX_TOKEN, config.TINKOFF_TRADE_TOKEN, config.TINKOFF_ACCOUNT_ID,
            config.TELEGRAM_BOT_TOKEN, config.CLOUDFLARE_URL, config.MARKET_DATA_EXPORT_TOKEN,
            config.ROBOT_CONTROL_TOKEN, config.MARKET_STRUCTURE_REDIS_PASSWORD)


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


def _percent(text: str) -> Decimal:
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"нужно число процентов, получено {text!r}") from None
    if not Decimal(0) < value < Decimal(50):
        raise argparse.ArgumentTypeError(f"процент должен быть больше 0 и меньше 50, получено {text}")
    return value


def _minutes(text: str) -> float:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужно число минут, получено {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("число минут должно быть больше нуля")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m src.finam", add_help=False)
    actions = parser.add_subparsers(dest="action")
    account = actions.add_parser("account", add_help=False)
    account.add_argument("instruments", nargs="*")
    account.add_argument("--days", type=_positive, default=30)
    probe = actions.add_parser("probe", add_help=False)
    probe.add_argument("instrument", nargs="?", default=DEFAULT_INSTRUMENT)
    probe.add_argument("--offset-pct", type=_percent, default=Decimal(3))
    stops = actions.add_parser("probe-stops", add_help=False)
    stops.add_argument("instrument", nargs="?", default=DEFAULT_INSTRUMENT)
    stops.add_argument("--offset-pct", type=_percent, default=Decimal(10))
    stops.add_argument("--side", choices=("buy", "sell"), default="buy")
    stops.add_argument("--keep", action="store_true")
    stops.add_argument("--tif", choices=("unset", "day", "gtc"), default="unset")
    trigger = actions.add_parser("probe-trigger", add_help=False)
    trigger.add_argument("instrument", nargs="?", default=DEFAULT_INSTRUMENT)
    trigger.add_argument("--ticks", type=_positive, default=1)
    trigger.add_argument("--side", choices=("buy", "sell"), default="buy")
    trigger.add_argument("--wait", type=_minutes, default=15)
    trigger.add_argument("--real", action="store_true")
    jwt = actions.add_parser("probe-jwt", add_help=False)
    jwt.add_argument("--minutes", type=_positive, default=20)
    limits = actions.add_parser("probe-limits", add_help=False)
    limits.add_argument("--calls", type=_positive, default=250)
    cleanup = actions.add_parser("cleanup", add_help=False)
    cleanup.add_argument("--order", default="")
    for sub in actions.choices.values():
        sub.add_argument("--demo", action="store_true")
        sub.error = parser.error
    return parser


def _params(args) -> str:
    skip = {"action"}
    parts = []
    for key, value in vars(args).items():
        if key in skip or value in (None, False, []):
            continue
        if isinstance(value, list):
            parts.append(" ".join(value))
            continue
        name = key.replace("_", "-")
        parts.append(f"--{name}" if value is True else f"--{name} {value}")
    return ", ".join(parts)


def _trigger_plan(args) -> str:
    side = "покупку" if args.side == "buy" else "продажу"
    where = "демо-счёте" if args.demo else "РЕАЛЬНОМ счёте"
    return (f"probe-trigger на {where}: стоп-заявка на {side} 1 лота {args.instrument} в {args.ticks} шаге(ах) от "
            f"рынка. Если цена дойдёт за {args.wait:g} мин, брокер совершит сделку, затем разведка закроет эту "
            "позицию по рынку. Будут комиссии за две сделки и возможный убыток на спреде.\n"
            f"Введите {CONFIRM_WORD}, чтобы продолжить: ")


def main(argv: Sequence[str] | None = None, *, env: FinamEnv | None = None,
         gateway_factory: Callable[..., FinamGateway] = FinamGateway,
         reference_factory: Callable[..., ReferenceGateway] | None = ReferenceGateway,
         out: Callable[[str], None] = print, ask: Callable[[str], str] = input,
         base_dir: Path | None = None, timing: Timing | None = None, now: datetime | None = None) -> int:
    env = env or FinamEnv()
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
    if args.action == "probe-trigger" and not args.demo and not args.real:
        out("probe-trigger совершает сделку. На реальном счёте добавьте --real, на демо — --demo.")
        return 2
    secret, account_id, secret_env, account_env = env.pick(args.demo)
    if not secret:
        out(f"Не задан {secret_env}")
        return 2
    if not account_id:
        out(f"Не задан {account_env}")
        return 2
    directory = (base_dir or config.runtime_dir()) / "finam"
    directory.mkdir(parents=True, exist_ok=True)
    setup_logging(service_uid="finam", log_file=str(directory / "finam.log"), level="DEBUG", secrets=_secrets(env))
    log.info("Команда Финама: %s, счёт %s%s", args.action, mask_account(account_id), " (демо)" if args.demo else "")
    if args.action == "probe-trigger":
        answer = ask(_trigger_plan(args)).strip()
        if answer != CONFIRM_WORD:
            out("Отменено: подтверждения нет, заявок не было.")
            return 2
    timing = timing or Timing()
    placeholder = "finam-demo-account" if args.demo else "finam-account"
    if args.action == "cleanup":
        reference = reference_factory(env.tinkoff_token) if env.tinkoff_token and reference_factory else None
        return _cleanup(secret, account_id, placeholder, gateway_factory, out, order_id=args.order.strip(),
                        reference=reference)
    stamp = timing.clock().strftime("%Y%m%d-%H%M%S")
    run_dir = directory / f"{args.action}-{stamp}"
    recorder = CallRecorder(run_dir / "responses.jsonl")
    gateway = gateway_factory(secret, account_id=account_id, account_placeholder=placeholder, recorder=recorder,
                              refresh_jwt=args.action != "probe-jwt")
    common = {"account_id": account_id, "demo": args.demo, "timing": timing, "params": _params(args), "out": out}
    reference = None
    if args.action in ("account", "probe", "probe-stops", "probe-trigger") and env.tinkoff_token and reference_factory:
        reference = reference_factory(env.tinkoff_token)
    instruments = Instruments(gateway, reference, now=now)
    run = _build(args, gateway, instruments, common, secret)
    out(f"{run.TITLE}: {'демо-счёт' if args.demo else 'счёт'} {mask_account(account_id)}.")
    code = 1
    try:
        with gateway:
            if reference is not None:
                with reference:
                    code = run.run()
            else:
                code = run.run()
    except BrokerCallError as exc:  # Auth при входе в шлюз: остальное ловит сама разведка
        code = run.fail_auth(exc)
    finally:
        run.write_report(run_dir)
        for line in run.report_lines():
            out(line)
        out(f"Отчёт и записи: {run_dir}")
    return code


def _build(args, gateway, instruments, common: dict, secret: str):
    if args.action == "account":
        return AccountRun(gateway, instruments=instruments, names=args.instruments or DEFAULT_INSTRUMENTS,
                          days=args.days, **common)
    if args.action == "probe":
        return LimitProbe(gateway, instruments=instruments, instrument=args.instrument, offset_pct=args.offset_pct,
                          **common)
    if args.action == "probe-stops":
        return StopProbe(gateway, instruments=instruments, instrument=args.instrument, offset_pct=args.offset_pct,
                         side=args.side, keep=args.keep, tif=args.tif, **common)
    if args.action == "probe-trigger":
        return TriggerProbe(gateway, instruments=instruments, instrument=args.instrument, ticks=args.ticks,
                            side=args.side, wait_min=args.wait, **common)
    if args.action == "probe-jwt":
        return JwtProbe(gateway, instruments=instruments, minutes=args.minutes, secret=secret, **common)
    return LimitsProbe(gateway, instruments=instruments, calls=args.calls, **common)


def _cleanup(secret, account_id, placeholder, gateway_factory, out, *, order_id: str = "", reference=None) -> int:
    """``cleanup``: справочник Т-Инвест нужен только для коротких имён фьючерсов в выводе."""
    from contextlib import nullcontext

    from src.finam.checks import Run

    gateway = gateway_factory(secret, account_id=account_id, account_placeholder=placeholder)
    try:
        with gateway, (reference if reference is not None else nullcontext()):
            names = Instruments(gateway, reference)
            check = Run(gateway, account_id=account_id, out=out)
            if not check._check_token():
                out(check.results["Ф0.1"].detail)
                return 1
            if check.token.readonly:
                out("Токен только на чтение — снимать заявки нельзя.")
                return 2
            if order_id:
                return cancel_by_number(gateway, account_id, order_id, out, names)
            return cleanup_orders(gateway, account_id, out, names)
    except BrokerCallError as exc:
        out(f"Не выполнено: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

