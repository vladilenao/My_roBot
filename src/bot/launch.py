"""Параметры запуска контуров и разрешение инструментов без консоли.

Робот стартует без параметров (add-history-contour); эмуляция и прогоны
истории приходят командами локального API. Объект ``LaunchRequest`` описывает
такой запуск. Без списка — все инструменты с привязками в конфигурации.
Эмуляция принимает только тикер акции и короткое имя контракта
(rework-contour-api); история — ещё код семейства (автоматическая цепочка)
и биржевой тикер контракта.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.data.timeutil import to_aware_utc, to_naive
from src.instruments.futures import FAMILIES, family_for, family_key
from src.instruments.model import SHARE_INSTRUMENT_TYPE, normalize_instrument
from src.instruments.selector import RTS_STOCK_TICKERS
from src.logging_setup import get_logger

log = get_logger(__name__)

MODE_EMULATION = "emulation"
MODE_HISTORY = "history"
SOURCE_CLI = "cli"
SOURCE_MENU = "menu"
SOURCE_API = "api"
ALL = "ALL"
HISTORY_DEFAULT_PAUSE = 1.0
_CONTRACT_TICKER = re.compile(r"[A-Za-z]+[FGHJKMNQUVXZ]\d{1,4}")


class RunConfigurationError(Exception):
    """Прогон не запускается: ответы оператора не задают рабочий диапазон."""


class LaunchError(ValueError):
    """Параметры запуска неверны; текст — для пользователя, без секретов."""


@dataclass(frozen=True)
class LaunchRequest:
    """Запуск робота: режим, диапазон истории и инструменты.

    ``instruments`` — кортеж токенов, ``(ALL,)`` для всех инструментов с
    привязками или ``None``, когда инструменты выбираются в меню.
    """

    mode: str
    source: str
    instruments: tuple[str, ...] | None = (ALL,)
    start: datetime | None = None
    end: datetime | None = None
    pause: float | None = None
    wait_command: bool = False
    # Ревизия из If-Match команды эмуляции (rework-contour-api); None — без проверки.
    expected_revision: int | None = None


@dataclass(frozen=True)
class Rejection:
    token: str
    reason: str


@dataclass(frozen=True)
class Chosen:
    """Что получилось из одного токена: имя для пользователя и срок контракта."""

    token: str
    name: str
    instrument_type: str
    days_to_expiry: int | None = None
    family: str | None = None
    automatic: bool = False

    def payload(self) -> dict:
        return {"token": self.token, "name": self.name, "instrument_type": self.instrument_type,
                "days_to_expiry": self.days_to_expiry}


@dataclass
class Resolution:
    """Итог разрешения: инструменты для сборки, выбранное и отказы."""

    instruments: object = None
    chosen: list[Chosen] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejections and bool(self.chosen)


@dataclass(frozen=True)
class _Target:
    kind: str  # share | family | contract
    token: str
    value: str
    family: str | None = None
    # Семейство выбрано правилом «все с привязками», а не названо в команде.
    automatic: bool = False


# --- даты и диапазон истории -------------------------------------------------

def parse_moment(raw: str) -> datetime:
    """Момент из строки оператора: без зоны — UTC, с зоной — приводим к UTC."""
    text = raw.strip()
    if not text:
        raise ValueError("Пустая дата — введите момент в формате ГГГГ-ММ-ДД ЧЧ:ММ.")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"Не разобрана дата «{text}» — нужен формат ГГГГ-ММ-ДД ЧЧ:ММ."
        ) from None
    return to_naive(to_aware_utc(moment))


def validate_range(start: datetime, end: datetime, pause: float) -> tuple[datetime, datetime, float]:
    if to_naive(start) >= to_naive(end):
        raise RunConfigurationError(
            f"Начало диапазона ({start:%Y-%m-%d %H:%M}) должно быть раньше конца "
            f"({end:%Y-%m-%d %H:%M}). Прогон не запущен."
        )
    return to_naive(start), to_naive(end), float(pause)


# --- командная строка ---------------------------------------------------------

START_HINT = ("Робот запускается без параметров и ждёт команд API: эмуляция — "
              "PUT /api/v1/emulation/target {\"mode\": \"on\", \"instruments\": [...]}, история — "
              "POST /api/v1/history {\"from\": ..., \"to\": ...}.")


def parse_launch_args(argv) -> None:
    """Робот стартует без параметров (add-history-contour): режим и инструменты приходят командами API.

    Любой параметр — ``LaunchError`` с подсказкой; служебные флаги (``--contour``,
    ``--version`` и другие) разбирает ``run.py`` до этого вызова.
    """
    args = [str(item) for item in argv]
    if args:
        raise LaunchError(f"Параметры {' '.join(args)} не принимаются. {START_HINT}")
    return None


def parse_instrument_tokens(raw) -> tuple[str, ...]:
    """Токены из строки или списка: регистр не важен, повторы убираются."""
    if raw is None:
        return (ALL,)
    parts = raw.replace(",", " ").split() if isinstance(raw, str) else list(raw)
    tokens: list[str] = []
    for part in parts:
        token = str(part).strip().upper()
        if token and token not in tokens:
            tokens.append(token)
    if not tokens:
        raise LaunchError("Список инструментов пуст.")
    if ALL in tokens and len(tokens) > 1:
        raise LaunchError("all нельзя сочетать с другими инструментами.")
    return tuple(tokens)


# --- разбор токенов без сети ----------------------------------------------------

def _family_has_assignments(family: str, future_strategies: dict) -> bool:
    return bool(future_strategies.get(family) or future_strategies.get(FAMILIES[family][0]))


def _all_targets(share_strategies: dict, future_strategies: dict) -> list[_Target]:
    targets = [_Target("share", ticker.upper(), ticker.upper()) for ticker in share_strategies if share_strategies[ticker]]
    families = []
    for key, assignments in future_strategies.items():
        try:
            family = family_key(key)
        except ValueError:
            log.warning("Секция [strategies.future.%s] не относится к известному семейству и пропущена", key)
            continue
        if assignments and family not in families:
            families.append(family)
    return targets + [_Target("family", family, family, family, automatic=True) for family in families]


def classify_tokens(tokens, share_strategies: dict, future_strategies: dict, *,
                    conflicts: bool = True) -> tuple[list[_Target], list[Rejection]]:
    """Токены → цели разрешения и отказы, без обращения к источнику.

    ``conflicts`` — отклонять семейство вместе с его контрактом (история); в
    эмуляции код семейства отклоняется сам, конфликта там нет.
    """
    shares = {ticker.upper() for ticker, items in share_strategies.items() if items}
    if tuple(tokens) == (ALL,):
        return _all_targets(share_strategies, future_strategies), []
    targets: list[_Target] = []
    rejections: list[Rejection] = []
    for token in tokens:
        if token in shares:
            targets.append(_Target("share", token, token))
            continue
        try:
            family = family_key(token)
        except ValueError:
            family = None
        if family is not None:
            if _family_has_assignments(family, future_strategies):
                targets.append(_Target("family", token, family, family))
            else:
                rejections.append(Rejection(token, "У семейства нет привязок в robot.toml."))
            continue
        contract_family = None
        if "-" in token or _CONTRACT_TICKER.fullmatch(token):
            try:
                contract_family = family_for(token, token)
            except ValueError:
                contract_family = None
        if contract_family is not None:
            if _family_has_assignments(contract_family, future_strategies):
                targets.append(_Target("contract", token, token, contract_family))
            else:
                rejections.append(Rejection(token, "У семейства контракта нет привязок в robot.toml."))
            continue
        if token in RTS_STOCK_TICKERS:
            rejections.append(Rejection(token, "У инструмента нет привязок в robot.toml."))
        else:
            rejections.append(Rejection(token, "Неизвестный инструмент: нет такой акции с привязками, семейства или контракта."))
    families = {target.family for target in targets if target.kind == "family"} if conflicts else set()
    for target in list(targets):
        if target.kind == "contract" and target.family in families:
            targets.remove(target)
            rejections.append(Rejection(target.token, f"Семейство {target.family} и его контракт нельзя выбрать в одном запуске."))
    return targets, rejections


# --- разрешение у источника -------------------------------------------------------

def _validate(validate, client, ticker, instrument_type, market_now, token, rejections):
    try:
        return validate(client, ticker, instrument_type, market_now=market_now)
    except ValueError:
        rejections.append(Rejection(token, "Брокер не подтвердил рабочий инструмент: свечи по нему недоступны."))
    except Exception as exc:  # сетевые и прочие сбои — только тип, без текста запроса
        log.warning("Проверка инструмента %s не выполнена: %s", token, type(exc).__name__)
        rejections.append(Rejection(token, f"Проверка у брокера не выполнена ({type(exc).__name__})."))
    return None


def _days_left(expiry: datetime, now: datetime) -> int:
    return int((expiry - now).total_seconds() // 86400)


def _match_contract(futures, token):
    for entry in futures:
        if str(entry[1]).upper() == token or str(entry[3]).upper() == token:
            return entry
    return None


def resolve_emulation(tokens, client, *, share_strategies: dict, future_strategies: dict,
                      block_days: int, market_now: datetime | None = None, validate=None,
                      active_futures=None) -> Resolution:
    """Эмуляция: акции, явные контракты коротким именем, у «всех с привязками» — ближайший контракт семейства.

    Код семейства и биржевой тикер контракта, названные в команде, отклоняются
    с подсказкой короткого имени (rework-contour-api). Результат
    ``instruments`` — 4-кортежи ``(label, ticker, type, short_name)``.
    """
    from src.api.instruments import find_working_instrument_with_name
    from src.instruments.selector import active_futures_with_expiry

    validate = validate or find_working_instrument_with_name
    active_futures = active_futures or active_futures_with_expiry
    now = to_naive(market_now) if market_now is not None else datetime.now(timezone.utc).replace(tzinfo=None)
    targets, rejections = classify_tokens(tokens, share_strategies, future_strategies, conflicts=False)
    result = Resolution(instruments=[], rejections=rejections)
    futures = None
    for target in targets:
        if target.kind == "share":
            found = _validate(validate, client, target.value, SHARE_INSTRUMENT_TYPE, market_now, target.token, result.rejections)
            if found is not None:
                short = found[1] or target.value
                result.instruments.append((target.value, target.value, SHARE_INSTRUMENT_TYPE, short))
                result.chosen.append(Chosen(target.token, short, SHARE_INSTRUMENT_TYPE))
            continue
        if futures is None:
            futures = list(active_futures(client, market_now))
        if target.kind == "family":
            candidates = [entry for entry in futures if _entry_family(entry) == target.family]
            entry = next((item for item in candidates if item[4] - now > timedelta(days=block_days)), None)
            if not target.automatic:
                hint = (f"ближайший допустимый — {entry[3]}." if entry is not None else
                        f"торгуемого контракта с экспирацией позже чем через {block_days} {_days_word(block_days)} нет.")
                result.rejections.append(Rejection(target.token, f"Код семейства не принимается — укажите контракт; {hint}"))
                continue
            if entry is None:
                result.rejections.append(Rejection(
                    target.token, f"Нет торгуемого контракта семейства с экспирацией позже чем через {block_days} {_days_word(block_days)}."))
                continue
        else:
            entry = _match_contract(futures, target.value)
            if entry is None:
                result.rejections.append(Rejection(
                    target.token, "Контракт не найден среди торгуемых стандартных контрактов ближайших шести месяцев."))
                continue
            if str(entry[3]).upper() != target.token:
                result.rejections.append(Rejection(
                    target.token, f"Биржевой тикер не принимается — укажите короткое имя {entry[3]}."))
                continue
        found = _validate(validate, client, entry[1], "future", market_now, _display_token(target, entry), result.rejections)
        if found is None:
            continue
        short = found[1] or entry[3]
        result.instruments.append((entry[0], entry[1], "future", short))
        result.chosen.append(Chosen(_display_token(target, entry), short, "future", _days_left(entry[4], now),
                                    target.family, automatic=target.kind == "family"))
    return result


def _entry_family(entry) -> str | None:
    try:
        return family_for(str(entry[1]), str(entry[3]))
    except ValueError:
        return None


def _display_token(target: _Target, entry) -> str:
    """Токен для пользователя: биржевой тикер контракта заменяется коротким именем."""
    if target.kind == "contract" and str(entry[1]).upper() == target.token:
        return str(entry[3])
    return target.token


def resolve_history(tokens, client, *, share_strategies: dict, future_strategies: dict,
                    market_now: datetime | None = None, validate=None, active_futures=None) -> Resolution:
    """История: семейства — автоматическая цепочка, акции и контракты — фиксированные."""
    from src.api.instruments import find_working_instrument_with_name
    from src.history.selection import HistorySelection
    from src.instruments.selector import fetch_active_futures

    validate = validate or find_working_instrument_with_name
    active_futures = active_futures or fetch_active_futures
    targets, rejections = classify_tokens(tokens, share_strategies, future_strategies)
    result = Resolution(rejections=rejections)
    fixed, families, catalog, futures = [], [], None, None
    for target in targets:
        if target.kind == "share":
            found = _validate(validate, client, target.value, SHARE_INSTRUMENT_TYPE, market_now, target.token, result.rejections)
            if found is not None:
                fixed.append(normalize_instrument((target.value, target.value, SHARE_INSTRUMENT_TYPE, found[1] or target.value)))
                result.chosen.append(Chosen(target.token, found[1] or target.value, SHARE_INSTRUMENT_TYPE))
        elif target.kind == "family":
            if catalog is None:
                catalog = tuple(client.futures_catalog())
            if not any(card.family == target.family and card.standard for card in catalog):
                result.rejections.append(Rejection(target.token, "В каталоге эмулятора нет стандартных контрактов семейства."))
            elif target.family not in families:
                families.append(target.family)
                result.chosen.append(Chosen(target.token, target.family, "future", family=target.family, automatic=True))
        else:
            if futures is None:
                futures = list(active_futures(client, market_now))
            entry = _match_contract(futures, target.value)
            if entry is None:
                result.rejections.append(Rejection(target.token, "Контракт не найден среди контрактов эмулятора на начало прогона."))
                continue
            found = _validate(validate, client, entry[1], "future", market_now, _display_token(target, entry), result.rejections)
            if found is not None:
                short = found[1] or entry[3]
                fixed.append(normalize_instrument((entry[0], entry[1], "future", short)))
                result.chosen.append(Chosen(_display_token(target, entry), short, "future", family=target.family))
    unique = {(item.ticker, item.instrument_type): item for item in fixed}
    result.instruments = HistorySelection(tuple(unique.values()), tuple(families), catalog or ())
    return result


# --- вывод для пользователя ---------------------------------------------------

def _days_word(days: int) -> str:
    return "сутки" if days % 10 == 1 and days % 100 != 11 else "суток"


def describe_choice(chosen: Chosen) -> str:
    if chosen.automatic and chosen.days_to_expiry is not None:
        return f"{chosen.token} → {chosen.name}, до экспирации {chosen.days_to_expiry} {_days_word(chosen.days_to_expiry)}"
    if chosen.automatic:
        return f"{chosen.name} — автоматическая цепочка контрактов"
    return chosen.name


def equivalent_command(request: LaunchRequest, chosen: list[Chosen]) -> str:
    """Тело ``PUT /api/v1/emulation/target``, повторяющее запуск эмуляции; контракты — коротким именем."""
    import json

    return json.dumps({"mode": "on", "instruments": [item.name for item in chosen]}, ensure_ascii=False)
