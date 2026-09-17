"""Загрузка конфигурации времени выполнения.

Приоритет значений (по убыванию):
  внешний `robot.toml` рядом с приложением
  -> вшитая в сборку таблица `default.toml` (для PyInstaller one-file — ``sys._MEIPASS``)
  -> дефолты, переданные в ``load_config`` (в dev это значения из кода).

Любая незнакомая секция, незнакомый ключ, неверный тип или недопустимое
значение приводят к ``ConfigError`` с указанием файла, чтобы ошибки конфига
всплывали сразу, а не в момент торговли.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from math import isfinite
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.decision.filters.triple_screen import TripleScreenParams, tf_hierarchy

CONFIG_FILENAME = "robot.toml"
BUNDLED_FILENAME = "default.toml"

# Маппинг секций TOML и их ключей на «плоские» ключи конфигурации.
_SECTIONS: dict[str, dict[str, str]] = {
    "robot": {
        "timeframe": "timeframe",
        "sleep_seconds": "sleep_seconds",
        "heartbeat_every_ticks": "heartbeat_every_ticks",
    },
    "tick": {
        "poll_secs": "tick_poll_secs",
        "timeout_secs": "tick_timeout_secs",
    },
    "instruments": {
        "fallback_type": "instrument_type",
        "fallback_ticker": "ticker",
    },
    "notifier": {
        "channel": "notifier",
    },
    "strategies": {
        "share": "share_strategies",
        "future": "future_strategies",
    },
    "strategies.filter": {
        "triple_screen": "triple_screen_params",
    },
    "logging": {
        "service_uid": "logging_service_uid",
        "file": "logging_file",
        "level": "logging_level",
        "max_bytes": "logging_max_bytes",
        "backup_count": "logging_backup_count",
        "audit_file": "audit_file",
        "audit_max_bytes": "audit_max_bytes",
        "audit_backup_count": "audit_backup_count",
    },
    "trading": {
        "initial_deposit": "initial_deposit",
        "max_risk_pct": "max_risk_pct",
        "journal_file": "journal_file",
        "positions_file": "positions_file",
        "clearing_times": "clearing_times",
        "database_file": "database_file",
        "risk_limits": "risk_limits",
    },
    "trade_management": {
        "profiles": "trade_management_profiles",
    },
}

_EXPECTED_TYPES: dict[str, type] = {
    "timeframe": str,
    "sleep_seconds": int,
    "heartbeat_every_ticks": int,
    "tick_poll_secs": int,
    "tick_timeout_secs": int,
    "instrument_type": str,
    "ticker": str,
    "notifier": str,
    "share_strategies": dict,
    "future_strategies": dict,
    "triple_screen_params": dict,
    "logging_service_uid": str,
    "logging_file": str,
    "logging_level": str,
    "logging_max_bytes": int,
    "logging_backup_count": int,
    "initial_deposit": int,
    "max_risk_pct": float,
    "journal_file": str,
    "positions_file": str,
    "clearing_times": list,
    "database_file": str,
    "audit_file": str,
    "audit_max_bytes": int,
    "audit_backup_count": int,
    "risk_limits": dict,
    "trade_management_profiles": dict,
}

_ALLOWED_NOTIFIER_VALUES = {"telegram", "console"}

# Таблица тикера в [strategies.*]: явные инлайн-привязки с устойчивым ID.
_STRATEGY_TABLE_KEYS = {"strategies", "timeframe"}
_STRATEGY_ENTRY_KEYS = {"id", "name", "management", "filter", "tf", "priority"}
_PROFILE_KEYS = {
    "levels_rr": {"type", "buffer_ticks", "target_R", "shares", "max_adds", "add_fraction"},
    "atr_trend": {"type", "atr_period", "initial_k", "trail_k", "tp1_R", "tp1_share", "max_adds", "add_fraction", "advance_R"},
    "ma_cloud": {"type", "ma_fast_period", "ma_slow_period", "buffer_ticks", "max_adds", "add_fraction"},
    "pattern_targets": {"type", "buffer_ticks", "fractions_to_D", "shares", "max_adds", "add_fraction"},
}
_RISK_LIMIT_KEYS = {"trade_pct", "instrument_pct", "portfolio_pct", "groups", "max_qty", "commission", "slippage"}
_PATTERN_STRATEGIES = {"harmonic_abcd"}


class ConfigError(RuntimeError):
    """Некорректный файл конфигурации."""


def _root_dir() -> Path:
    """Корень проекта (каталог, содержащий пакет ``src``)."""
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Каталог, в котором ищется внешний конфиг.

    Для PyInstaller-сборки — папка исполняемого файла; в разработке — корень
    проекта. Никогда не ``cwd``: запуск из другой папки не должен ломать поиск.
    """
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(sys.executable))
    return _root_dir()


def _type_name(expected: type) -> str:
    return {str: "строка", int: "целое число", dict: "таблица"}.get(
        expected, expected.__name__
    )


def derived_positions_file(journal_file: str) -> str:
    """Имя файла карточек по умолчанию: суффикс `_positions` перед расширением."""
    path = Path(journal_file)
    return f"{path.stem}_positions{path.suffix}"


def _validate_strategy_entry(item: Any, ticker: str, path: Path) -> None:
    """Проверка одной явной привязки стратегии."""
    if isinstance(item, str):
        raise ConfigError(f"{path}: привязка стратегии для {ticker!r} должна быть инлайн-таблицей с id и management")
    if not isinstance(item, dict):
        raise ConfigError(
            f"{path}: элемент strategies для {ticker!r} должен быть инлайн-таблицей, получено {type(item).__name__}"
        )
    unknown = set(item) - _STRATEGY_ENTRY_KEYS
    if unknown:
        raise ConfigError(
            f"{path}: привязка стратегии для {ticker!r}: незнакомые ключи "
            f"{sorted(unknown)}; допустимые: {sorted(_STRATEGY_ENTRY_KEYS)}"
        )
    for required in ("id", "name", "management"):
        if not isinstance(item.get(required), str) or not item[required]:
            raise ConfigError(
                f"{path}: привязка стратегии для {ticker!r} требует непустой строковый ключ {required!r}"
            )
    if "priority" in item and (not isinstance(item["priority"], int) or isinstance(item["priority"], bool)):
        raise ConfigError(
            f"{path}: привязка стратегии для {ticker!r}: ключ 'priority' должен быть целым числом"
        )
    if "filter" in item and not isinstance(item["filter"], str):
        raise ConfigError(
            f"{path}: привязка стратегии для {ticker!r}: ключ 'filter' "
            f"должен быть строкой"
        )
    if "tf" in item and not isinstance(item["tf"], str):
        raise ConfigError(
            f"{path}: привязка стратегии для {ticker!r}: ключ 'tf' "
            f"должен быть строкой"
        )


def _validate_strategies(value: dict, key: str, path: Path) -> dict[str, dict]:
    """Проверка словаря привязок и нормализация таблиц тикеров.

    Возвращает ``{тикер: {"strategies": [...], "timeframe": str | None}}``.
    Допустимость значений таймфрейма проверяется доменом (``src.config``)
    по словарю TIMEFRAMES — загрузчик отвечает только за форму.
    """
    unwrapped: dict[str, dict] = {}
    for ticker, table in value.items():
        if not isinstance(table, dict):
            raise ConfigError(
                f"{path}: [{key}] привязка для {ticker!r} должна быть таблицей "
                f"с ключом 'strategies'; плоский список более не поддерживается"
            )
        unknown = set(table) - _STRATEGY_TABLE_KEYS
        if unknown:
            raise ConfigError(
                f"{path}: [{key}.{ticker}] незнакомые ключи {sorted(unknown)}; "
                f"допустимые: {sorted(_STRATEGY_TABLE_KEYS)}"
            )
        strategies = table.get("strategies")
        if strategies is None:
            raise ConfigError(
                f"{path}: [{key}.{ticker}] отсутствует обязательный ключ 'strategies'"
            )
        if not isinstance(strategies, list):
            raise ConfigError(
                f"{path}: [{key}.{ticker}] strategies должен быть массивом"
            )
        timeframe = table.get("timeframe")
        if timeframe is not None and not isinstance(timeframe, str):
            raise ConfigError(
                f"{path}: [{key}.{ticker}] timeframe должен быть строкой"
            )
        for item in strategies:
            _validate_strategy_entry(item, ticker, path)
        unwrapped[ticker] = {"strategies": strategies, "timeframe": timeframe}
    return unwrapped


def _finite_number(value: Any, key: str, path: Path, *, positive: bool = False, non_negative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ConfigError(f"{path}: {key} должен быть конечным числом")
    if positive and value <= 0:
        raise ConfigError(f"{path}: {key} должен быть > 0")
    if non_negative and value < 0:
        raise ConfigError(f"{path}: {key} должен быть >= 0")


def _validate_shares(value: Any, key: str, path: Path, count: int | None = None) -> None:
    if not isinstance(value, list) or not value or (count is not None and len(value) != count):
        raise ConfigError(f"{path}: {key} должен быть непустым списком корректной длины")
    for share in value:
        _finite_number(share, key, path, positive=True)
        if share > 1:
            raise ConfigError(f"{path}: {key} должен содержать доли в (0, 1]")
    if sum(value) > 1:
        raise ConfigError(f"{path}: {key} не должен давать сумму больше 1")


def _validate_profile_parameters(name: str, value: dict[str, Any], path: Path) -> None:
    allowed = _PROFILE_KEYS.get(name)
    if allowed is None:
        raise ConfigError(f"{path}: неизвестный профиль управления {name!r}")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"{path}: [trade_management.profiles.{name}] незнакомые ключи {sorted(unknown)}")
    if value.get("type") != name:
        raise ConfigError(f"{path}: [trade_management.profiles.{name}] ключ 'type' должен быть {name!r}")
    for key in ("buffer_ticks", "max_adds", "atr_period", "ma_fast_period", "ma_slow_period"):
        if key in value and (isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < (1 if key.endswith("period") else 0)):
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] {key} должен быть корректным целым числом")
    for key in ("initial_k", "trail_k", "tp1_R", "add_fraction", "advance_R", "tp1_share"):
        if key in value:
            _finite_number(value[key], f"[trade_management.profiles.{name}] {key}", path, positive=True)
            if key.endswith("share") or key == "add_fraction":
                if value[key] > 1:
                    raise ConfigError(f"{path}: [trade_management.profiles.{name}] {key} должен быть в (0, 1]")
    if name == "levels_rr":
        targets = value.get("target_R")
        if not isinstance(targets, list) or not targets:
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] target_R должен быть непустым списком")
        for target in targets:
            _finite_number(target, f"[trade_management.profiles.{name}] target_R", path, positive=True)
        if any(right <= left for left, right in zip(targets, targets[1:])):
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] target_R должен возрастать")
        _validate_shares(value.get("shares"), f"[trade_management.profiles.{name}] shares", path, len(targets))
    if name == "pattern_targets":
        fractions = value.get("fractions_to_D")
        if not isinstance(fractions, list) or len(fractions) != 2:
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] fractions_to_D должен содержать две доли")
        for fraction in fractions:
            _finite_number(fraction, f"[trade_management.profiles.{name}] fractions_to_D", path, positive=True)
            if fraction > 1:
                raise ConfigError(f"{path}: [trade_management.profiles.{name}] fractions_to_D должен быть в (0, 1]")
        if fractions[0] >= fractions[1]:
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] fractions_to_D должен возрастать")
        _validate_shares(value.get("shares"), f"[trade_management.profiles.{name}] shares", path, 2)
    if name == "atr_trend" and "atr_period" not in value:
        raise ConfigError(f"{path}: [trade_management.profiles.{name}] atr_period задаёт необходимый прогрев")
    if name == "ma_cloud":
        fast, slow = value.get("ma_fast_period"), value.get("ma_slow_period")
        if fast is None or slow is None or fast >= slow:
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] требует ma_fast_period < ma_slow_period для прогрева")


def _validate_trade_management_profiles(value: dict, path: Path) -> dict[str, dict]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [trade_management.profiles] должна быть таблицей")
    for name, parameters in value.items():
        if not isinstance(parameters, dict):
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] должна быть таблицей")
        _validate_profile_parameters(name, parameters, path)
    return value


def _validate_risk_limits(value: dict, path: Path) -> dict[str, Any]:
    unknown = set(value) - _RISK_LIMIT_KEYS
    if unknown:
        raise ConfigError(f"{path}: [trading.risk_limits] незнакомые ключи {sorted(unknown)}")
    for key in ("trade_pct", "instrument_pct", "portfolio_pct", "commission", "slippage"):
        if key in value:
            _finite_number(value[key], f"[trading.risk_limits] {key}", path, positive=key.endswith("_pct"), non_negative=not key.endswith("_pct"))
    if "max_qty" in value and (isinstance(value["max_qty"], bool) or not isinstance(value["max_qty"], int) or value["max_qty"] <= 0):
        raise ConfigError(f"{path}: [trading.risk_limits] max_qty должен быть целым числом > 0")
    groups = value.get("groups", {})
    if not isinstance(groups, dict):
        raise ConfigError(f"{path}: [trading.risk_limits] groups должна быть таблицей")
    for group, limit in groups.items():
        if not isinstance(group, str) or not group:
            raise ConfigError(f"{path}: [trading.risk_limits] groups содержит пустое имя")
        _finite_number(limit, f"[trading.risk_limits.groups] {group}", path, positive=True)
    return value


def _validate(flat: dict[str, Any], path: Path) -> dict[str, Any]:
    """Проверка типов и допустимых значений ключей конфигурации."""
    cleaned: dict[str, Any] = {}
    for key, value in flat.items():
        expected = _EXPECTED_TYPES[key]
        if not isinstance(value, expected):
            raise ConfigError(
                f"{path}: [{key}] ожидается {_type_name(expected)}, "
                f"получено {type(value).__name__}"
            )
        if key == "notifier" and value not in _ALLOWED_NOTIFIER_VALUES:
            raise ConfigError(
                f"{path}: [notifier] channel должен быть одним из "
                f"{sorted(_ALLOWED_NOTIFIER_VALUES)}, получено {value!r}"
            )
        if key == "triple_screen_params":
            cleaned[key] = _validate_triple_screen_params(value, path)
            continue
        if key == "trade_management_profiles":
            cleaned[key] = _validate_trade_management_profiles(value, path)
            continue
        if key == "risk_limits":
            cleaned[key] = _validate_risk_limits(value, path)
            continue
        if key == "initial_deposit":
            if isinstance(value, bool):
                raise ConfigError(
                    f"{path}: [trading] initial_deposit ожидается целое число > 0, "
                    f"получено {value!r}"
                )
            if value <= 0:
                raise ConfigError(
                    f"{path}: [trading] initial_deposit должен быть > 0, "
                    f"получено {value!r}"
                )
        if key == "max_risk_pct":
            if isinstance(value, bool) or not (0 < value <= 100):
                raise ConfigError(
                    f"{path}: [trading] max_risk_pct должен быть дробным в (0, 100], "
                    f"получено {value!r}"
                )
        if key == "clearing_times":
            _validate_clearing_times(value, path)
        if key == "positions_file" and not value:
            raise ConfigError(
                f"{path}: [trading] positions_file должен быть непустой строкой, "
                f"получено {value!r}"
            )
        if key in {"database_file", "audit_file", "journal_file"} and not value:
            raise ConfigError(f"{path}: [{key}] должен быть непустой строкой")
        if key in {"audit_max_bytes", "audit_backup_count"} and value < 0:
            raise ConfigError(f"{path}: [{key}] должен быть неотрицательным целым числом")
        if expected is dict:
            value = _validate_strategies(value, key, path)
        cleaned[key] = value
    return cleaned


_CLEARING_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_clearing_times(value: Any, path: Path) -> None:
    """Проверка `clearing_times`: список строк `HH:MM` (непустой)."""
    if not value:
        raise ConfigError(
            f"{path}: [trading] clearing_times должен быть непустым списком"
        )
    for item in value:
        if not isinstance(item, str) or not _CLEARING_TIME_RE.match(item):
            raise ConfigError(
                f"{path}: [trading] clearing_times: элемент {item!r} должен быть "
                f"времени в формате HH:MM"
            )


def _validate_triple_screen_params(value: Any, path: Path) -> dict[str, Any]:
    """Проверка секции `[strategies.filter.triple_screen]`: строгие ключи и типы."""
    if not isinstance(value, dict):
        raise ConfigError(
            f"{path}: [strategies.filter.triple_screen] должна быть таблицей"
        )
    non_int = {
        key: val
        for key, val in value.items()
        if isinstance(val, bool) or not isinstance(val, int)
    }
    if non_int:
        raise ConfigError(
            f"{path}: [strategies.filter.triple_screen] ключи "
            f"{sorted(non_int)} должны быть целыми числами"
        )
    try:
        TripleScreenParams.from_config(dict(value))
    except ValueError as exc:
        raise ConfigError(
            f"{path}: [strategies.filter.triple_screen] {exc}"
        ) from exc
    return dict(value)


def validate_triple_screen_hierarchy(
    bindings: Mapping[str, list[Any]],
    multiplier: int,
    ladder: Sequence[str],
    path: Path,
) -> None:
    """Проверка иерархии ТФ привязок с профилем `triple_screen` (дефолты Элдера).

    Для каждой привязки ``filter = "triple_screen"`` вычисляется ``tf_hierarchy``;
    при выходе шага множителя за пределы лестницы ``TIMEFRAMES`` — ``ConfigError``
    с указанием привязки.
    """
    for ticker, items in bindings.items():
        for item in items:
            if getattr(item, "filter_profile", None) != "triple_screen":
                continue
            try:
                tf_hierarchy(item.timeframe, multiplier, ladder)
            except ValueError as exc:
                raise ConfigError(
                    f"{path}: привязка {ticker!r} (стратегия {item.strategy}, "
                    f"таймфрейм {item.timeframe!r}) несовместима с профилем "
                    f"triple_screen: {exc}"
                ) from exc


def _parse(path: Path) -> dict[str, Any]:
    """Чтение одного TOML-файла в «плоские» ключи конфигурации."""
    try:
        with open(path, "rb") as fh:
            data: dict[str, Any] = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: ошибка синтаксиса TOML: {exc}") from exc

    flat: dict[str, Any] = {}
    for section, mapping in data.items():
        if section not in _SECTIONS:
            raise ConfigError(
                f"{path}: незнакомая секция [{section}]; "
                f"допустимые: {', '.join(sorted(_SECTIONS))}"
            )
        if not isinstance(mapping, dict):
            raise ConfigError(f"{path}: секция [{section}] должна быть таблицей")
        allowed_keys = _SECTIONS[section]
        for key, value in mapping.items():
            if key == "filter" and section == "strategies":
                flat["triple_screen_params"] = _parse_triple_screen_section(value, path)
                continue
            target = allowed_keys.get(key)
            if target is None:
                raise ConfigError(
                    f"{path}: [{section}] незнакомый ключ {key!r}; "
                    f"допустимые: {', '.join(sorted(allowed_keys))}"
                )
            flat[target] = value
    return _validate(flat, path)


def _validate_combined_config(config: Mapping[str, Any], path: Path) -> None:
    """Validate constraints that span TOML sections and configuration files."""
    assignment_ids: set[str] = set()
    assignments: list[dict[str, Any]] = []
    for source in ("share_strategies", "future_strategies"):
        for table in config.get(source, {}).values():
            if not isinstance(table, dict) or not isinstance(table.get("strategies"), list):
                continue
            for assignment in table["strategies"]:
                if not isinstance(assignment, dict):
                    continue
                assignment_id = assignment["id"]
                if assignment_id in assignment_ids:
                    raise ConfigError(f"{path}: повторяющийся id привязки {assignment_id!r}")
                assignment_ids.add(assignment_id)
                assignments.append(assignment)

    profiles = config.get("trade_management_profiles")
    if profiles is not None:
        for assignment in assignments:
            management = assignment["management"]
            if management not in profiles:
                raise ConfigError(f"{path}: привязка {assignment['id']!r} ссылается на неизвестный management {management!r}")
            if profiles[management]["type"] == "pattern_targets" and assignment["name"] not in _PATTERN_STRATEGIES:
                raise ConfigError(f"{path}: привязка {assignment['id']!r}: pattern_targets несовместим со стратегией {assignment['name']!r}")

    database_file = config.get("database_file")
    if database_file is not None:
        for key in ("journal_file", "positions_file", "audit_file"):
            if config.get(key) is not None and Path(config[key]).resolve() == Path(database_file).resolve():
                raise ConfigError(f"{path}: [trading] database_file не должен совпадать с {key}")


def _parse_triple_screen_section(value: Any, path: Path) -> dict[str, Any]:
    """Извлечение таблицы `[strategies.filter.triple_screen]` из вложенной секции."""
    if not isinstance(value, dict):
        raise ConfigError(
            f"{path}: [strategies.filter] должна быть таблицей с секцией triple_screen"
        )
    unknown = set(value) - {"triple_screen"}
    if unknown:
        raise ConfigError(
            f"{path}: [strategies.filter] незнакомые секции {sorted(unknown)}; "
            f"допустимые: ['triple_screen']"
        )
    params = value.get("triple_screen")
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ConfigError(
            f"{path}: [strategies.filter.triple_screen] должна быть таблицей"
        )
    return dict(params)


def _candidate_files(
    config_file: str | os.PathLike | None = None,
    bundled_file: str | os.PathLike | None = None,
) -> list[Path]:
    """Список файлов конфигурации в порядке применения (сильнее — последним)."""
    if config_file is not None:
        files: list[Path] = [Path(config_file)]
        if bundled_file is not None:
            files.insert(0, Path(bundled_file))
        return files

    files: list[Path] = []
    external = app_dir() / CONFIG_FILENAME
    if external.is_file():
        files.append(external)

    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", os.getcwd())) / BUNDLED_FILENAME
        if bundled.is_file():
            files.append(bundled)
    else:
        dev_default = _root_dir() / BUNDLED_FILENAME
        if dev_default.is_file():
            files.append(dev_default)

    return files


def load_config(
    defaults: Mapping[str, Any] | None = None,
    config_file: str | os.PathLike | None = None,
    bundled_file: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Загрузка конфигурации с учётом приоритетов.

    ``defaults`` — значения из кода (последний якорь). Для тестов можно задать
    ``config_file``/``bundled_file`` явно; иначе пути резолвятся автоматически.
    """
    result: dict[str, Any] = dict(defaults or {})
    for path in _candidate_files(config_file, bundled_file):
        if not path.is_file():
            continue
        result.update(_parse(path))
    _validate_combined_config(result, Path(config_file or CONFIG_FILENAME))
    return result
