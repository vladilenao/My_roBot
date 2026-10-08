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
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.decision.filters.triple_screen import TripleScreenParams, tf_hierarchy
from src.events.types import EVENT_TYPE_NAMES
from src.logging_setup import get_logger

CONFIG_FILENAME = "robot.toml"
BUNDLED_FILENAME = "default.toml"

# Маппинг секций TOML и их ключей на «плоские» ключи конфигурации.
_SECTIONS: dict[str, dict[str, str]] = {
    "robot": {
        "timeframe": "timeframe",
        "sleep_seconds": "sleep_seconds",
        "heartbeat_every_ticks": "heartbeat_every_ticks",
        "data_dir": "data_dir",
    },
    "tick": {
        "poll_secs": "tick_poll_secs",
        "timeout_secs": "tick_timeout_secs",
        "catch_up_bars": "tick_catch_up_bars",
    },
    "instruments": {
        "fallback_type": "instrument_type",
        "fallback_ticker": "ticker",
    },
    "notifier": {
        "channels": "notifier_channels",
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
        "directions": "directions",
        "contract_expiry_block_days": "contract_expiry_block_days",
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
    "tick_catch_up_bars": int,
    "instrument_type": str,
    "ticker": str,
    "data_dir": str,
    "notifier_channels": list,
    "notifier_console_events": list,
    "notifier_telegram_events": list,
    "notifier_telegram_request_timeout": int,
    "notifier_telegram_max_transport_attempts": int,
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
    "directions": dict,
    "trade_management_profiles": dict,
    "contract_expiry_block_days": int,
}

_ALLOWED_NOTIFIER_VALUES = {"telegram", "console"}
_NOTIFIER_CHANNELS_KEYS = {"notifier_channels"}
_NOTIFIER_EVENTS_KEYS = {"notifier_console_events", "notifier_telegram_events"}
_NOTIFIER_EVENT_KEYS = {"console": "notifier_console_events", "telegram": "notifier_telegram_events"}
# Служебные ключи подсекции [notifier.<канал>]: сейчас только у telegram.
# Ключи пишутся в плоские имена notifier_telegram_<ключ>.
_NOTIFIER_TELEGRAM_EXTRA_KEYS = {"request_timeout", "max_transport_attempts"}

# Таблица тикера в [strategies.*]: явные инлайн-привязки с устойчивым ID.
_STRATEGY_TABLE_KEYS = {"strategies", "timeframe"}
_STRATEGY_ENTRY_KEYS = {"id", "name", "management", "filter", "tf", "priority"}
_STOP_GEOMETRY_KEYS = frozenset(
    {"min_stop_atr", "min_stop_ticks", "stop_beyond_bar", "max_stop_atr"}
)
_PROFILE_KEYS = {
    "levels_rr": {"type", "buffer_ticks", "target_R", "shares", "max_adds", "add_fraction", "min_be_r"} | _STOP_GEOMETRY_KEYS,
    "atr_trend": {"type", "atr_period", "initial_k", "trail_k", "target_R", "shares", "max_adds", "add_fraction", "advance_R"} | _STOP_GEOMETRY_KEYS,
    "ma_cloud": {"type", "ma_fast_period", "ma_slow_period", "buffer_ticks", "max_adds", "add_fraction"} | _STOP_GEOMETRY_KEYS,
    "pattern_targets": {"type", "buffer_ticks", "fractions_to_D", "shares", "max_adds", "add_fraction", "min_be_r"} | _STOP_GEOMETRY_KEYS,
}
_LEGACY_RISK_KEYS = frozenset({"trade_pct", "instrument_pct", "groups"})
_RISK_DEFAULTS = {
    "portfolio_pct": 2.0,
    "commission": 1.5,
    "slippage": 1.0,
    "min_trade_risk_pct": 0.0,
    "min_risk_cost_ratio": 2.0,
    "min_net_payoff": 1.5,
    "max_slippage_r": 0.25,
}
_RISK_LIMIT_KEYS = set(_RISK_DEFAULTS) | {"max_qty", "slippage_tolerance"}
_ALLOWED_DIRECTIONS = frozenset({"long", "short"})
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
    if sum((Decimal(str(item)) for item in value), Decimal(0)) > 1:
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
    if name in {"levels_rr", "pattern_targets"}:
        value.setdefault("min_be_r", 1.5)
        _finite_number(value["min_be_r"], f"[trade_management.profiles.{name}] min_be_r", path, non_negative=True)
    if name in {"levels_rr", "atr_trend"}:
        targets = value.get("target_R")
        if not isinstance(targets, list) or not targets:
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] target_R должен быть непустым списком")
        for target in targets:
            _finite_number(target, f"[trade_management.profiles.{name}] target_R", path, positive=True)
        if any(right <= left for left, right in zip(targets, targets[1:])):
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] target_R должен возрастать")
        _validate_shares(value.get("shares"), f"[trade_management.profiles.{name}] shares", path, len(targets))
        if name == "atr_trend" and sum((Decimal(str(x)) for x in value["shares"]), Decimal(0)) >= 1:
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] shares должна оставлять остаток под трейлинг (сумма < 1)")
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
    _validate_stop_geometry(name, value, path)


def _validate_stop_geometry(name: str, value: dict[str, Any], path: Path) -> None:
    """Общие границы стоп-геометрии: неотрицательные числа, целые тики, согласованный потолок.

    Потолок уже пола быть не может: иначе правило применяло бы две несовместимые
    границы и итоговое расстояние зависело бы от порядка их применения.
    """
    where = f"[trade_management.profiles.{name}]"
    for key in ("min_stop_atr", "stop_beyond_bar", "max_stop_atr"):
        if key in value:
            _finite_number(value[key], f"{where} {key}", path, non_negative=True)
    if "min_stop_ticks" in value:
        ticks = value["min_stop_ticks"]
        if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
            raise ConfigError(f"{path}: {where} min_stop_ticks должен быть целым числом >= 0")
    min_stop_atr, max_stop_atr = value.get("min_stop_atr"), value.get("max_stop_atr")
    if max_stop_atr is not None and min_stop_atr is not None and max_stop_atr < min_stop_atr:
        raise ConfigError(
            f"{path}: {where} max_stop_atr ({max_stop_atr}) не может быть меньше "
            f"min_stop_atr ({min_stop_atr})"
        )


def _validate_trade_management_profiles(value: dict, path: Path) -> dict[str, dict]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [trade_management.profiles] должна быть таблицей")
    for name, parameters in value.items():
        if not isinstance(parameters, dict):
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] должна быть таблицей")
        _validate_profile_parameters(name, parameters, path)
    return value


def _validate_risk_limits(value: dict, path: Path) -> dict[str, Any]:
    _reject_legacy_risk_keys(value, path)
    unknown = set(value) - _RISK_LIMIT_KEYS
    if unknown:
        raise ConfigError(f"{path}: [trading.risk_limits] незнакомые ключи {sorted(unknown)}")
    value = {**_RISK_DEFAULTS, **value}
    for key in ("portfolio_pct", "commission", "slippage", "slippage_tolerance", "min_risk_cost_ratio", "min_net_payoff", "max_slippage_r"):
        if key in value:
            _finite_number(value[key], f"[trading.risk_limits] {key}", path, non_negative=True)
    if value["portfolio_pct"] > 100:
        raise ConfigError(f"{path}: [trading.risk_limits] portfolio_pct должен быть в [0, 100]")
    if "min_trade_risk_pct" in value:
        _finite_number(value["min_trade_risk_pct"], "[trading.risk_limits] min_trade_risk_pct", path, non_negative=True)
        if value["min_trade_risk_pct"] > value["portfolio_pct"]:
            raise ConfigError(
                f"{path}: [trading.risk_limits] min_trade_risk_pct "
                f"({value['min_trade_risk_pct']}) не может превышать portfolio_pct ({value['portfolio_pct']})"
            )
    if "max_qty" in value and (isinstance(value["max_qty"], bool) or not isinstance(value["max_qty"], int) or value["max_qty"] <= 0):
        raise ConfigError(f"{path}: [trading.risk_limits] max_qty должен быть целым числом > 0")
    return value


def _validate_directions(value: dict, path: Path) -> dict[str, list[str]]:
    """Проверка секции `[trading.directions]`: тип инструмента → список long/short."""
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [trading.directions] должна быть таблицей")
    cleaned: dict[str, list[str]] = {}
    for instrument_type, directions in value.items():
        key = f"[trading.directions] {instrument_type}"
        if not isinstance(directions, list) or not directions:
            raise ConfigError(f"{path}: {key} должен быть непустым списком направлений long/short")
        invalid = [
            item for item in directions
            if not isinstance(item, str) or item not in _ALLOWED_DIRECTIONS
        ]
        if invalid:
            raise ConfigError(f"{path}: {key} содержит недопустимые направления {sorted(invalid)}; допустимы только long/short в нижнем регистре")
        cleaned[instrument_type] = list(dict.fromkeys(directions))
    return cleaned


def _reject_legacy_risk_keys(value: Mapping[str, Any], path: Path) -> None:
    legacy = set(value) & _LEGACY_RISK_KEYS
    if legacy:
        raise ConfigError(
            f"{path}: [trading.risk_limits] устаревшие отдельные лимиты {sorted(legacy)}. "
            "Удалите trade_pct, instrument_pct и groups; проверьте и явно задайте "
            "portfolio_pct — единый риск всего портфеля (поставляемый дефолт 2%). "
            "Значения старых лимитов автоматически не переносятся."
        )


def _merge_management_tables(flat: dict[str, Any], inherited: Mapping[str, Any], path: Path) -> None:
    """Нормализовать явную форму целей до наложения дефолтов предыдущего слоя."""
    risk = flat.get("risk_limits")
    if isinstance(risk, dict):
        _reject_legacy_risk_keys(risk, path)
        flat["risk_limits"] = {**inherited.get("risk_limits", {}), **risk}
    profiles = flat.get("trade_management_profiles")
    if not isinstance(profiles, dict):
        return
    merged = dict(inherited.get("trade_management_profiles", {}))
    for name, supplied in profiles.items():
        if not isinstance(supplied, dict):
            raise ConfigError(f"{path}: [trade_management.profiles.{name}] должна быть таблицей")
        supplied = dict(supplied)
        if name == "atr_trend":
            old_keys = set(supplied) & {"tp1_R", "tp1_share"}
            new_keys = set(supplied) & {"target_R", "shares"}
            if old_keys and new_keys:
                raise ConfigError(f"{path}: [trade_management.profiles.{name}] конфликт target_R/shares и tp1_R/tp1_share")
            if old_keys:
                if len(old_keys) != 2:
                    raise ConfigError(f"{path}: [trade_management.profiles.{name}] задайте полную пару tp1_R и tp1_share")
                supplied["target_R"] = [supplied.pop("tp1_R")]
                supplied["shares"] = [supplied.pop("tp1_share")]
        parameters = {**merged.get(name, {}), **supplied}
        if name == "atr_trend":
            parameters.setdefault("target_R", [1.0, 2.0])
            parameters.setdefault("shares", [0.25, 0.25])
        merged[name] = parameters
    flat["trade_management_profiles"] = merged


def _validate_notifier_key(key: str, value: Any, path: Path) -> list[str]:
    """Список каналов уведомлений: непустой, без повторов, из известных."""
    if not value:
        raise ConfigError(
            f"{path}: [notifier] channels не может быть пустым: "
            f"укажите хотя бы один канал из {sorted(_ALLOWED_NOTIFIER_VALUES)}"
        )
    if len(set(value)) != len(value):
        duplicates = sorted({item for item in value if value.count(item) > 1})
        raise ConfigError(f"{path}: [notifier] channels повторяет каналы: {duplicates}")
    unknown = sorted(set(value) - _ALLOWED_NOTIFIER_VALUES)
    if unknown:
        raise ConfigError(
            f"{path}: [notifier] channels: неизвестные каналы {unknown}; "
            f"доступны: {sorted(_ALLOWED_NOTIFIER_VALUES)}"
        )
    return list(value)


def _validate_notifier_events(key: str, value: Any, path: Path) -> list[str]:
    """Список типов событий канала: непустой, без повторов, из каталога."""
    where = "notifier.console" if key.endswith("console_events") else "notifier.telegram"
    if not value:
        raise ConfigError(
            f"{path}: [{where}] events не может быть пустым: "
            f"укажите хотя бы один тип из каталога"
        )
    if len(set(value)) != len(value):
        duplicates = sorted({item for item in value if value.count(item) > 1})
        raise ConfigError(f"{path}: [{where}] events повторяет типы: {duplicates}")
    unknown = sorted(set(value) - set(EVENT_TYPE_NAMES))
    if unknown:
        raise ConfigError(
            f"{path}: [{where}] events: неизвестные типы {unknown}; "
            f"допустимые: {sorted(EVENT_TYPE_NAMES)}"
        )
    return list(value)


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
        if key in _NOTIFIER_CHANNELS_KEYS:
            cleaned[key] = _validate_notifier_key(key, value, path)
            continue
        if key in _NOTIFIER_EVENTS_KEYS:
            cleaned[key] = _validate_notifier_events(key, value, path)
            continue
        if key in {
            "notifier_telegram_request_timeout",
            "notifier_telegram_max_transport_attempts",
        } and (isinstance(value, bool) or value < 1):
            option = key.removeprefix("notifier_telegram_")
            raise ConfigError(
                f"{path}: [notifier.telegram] {option} должен быть целым числом > 0, "
                f"получено {value!r}"
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
        if key == "directions":
            cleaned[key] = _validate_directions(value, path)
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
        if key in {"database_file", "audit_file", "journal_file", "data_dir"} and not value:
            raise ConfigError(f"{path}: [{key}] должен быть непустой строкой")
        if key in {"audit_max_bytes", "audit_backup_count"} and value < 0:
            raise ConfigError(f"{path}: [{key}] должен быть неотрицательным целым числом")
        if key == "tick_catch_up_bars" and (isinstance(value, bool) or value < 0):
            raise ConfigError(
                f"{path}: [tick] catch_up_bars должен быть неотрицательным целым числом"
            )
        if key == "contract_expiry_block_days" and (isinstance(value, bool) or value < 0):
            raise ConfigError(
                f"{path}: [trading] contract_expiry_block_days должен быть неотрицательным целым числом"
            )
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


def _parse_notifier_subsection(
    name: str, value: Any, path: Path
) -> tuple[str, dict[str, Any]]:
    """Подсекция ``[notifier.<канал>]``: ключ ``events`` и служебные ключи.

    Возвращает целевой ключ событий и дополнительные плоские ключи со
    значениями (сейчас это только ``request_timeout`` у telegram).
    """
    target = _NOTIFIER_EVENT_KEYS.get(name)
    if target is None:
        raise ConfigError(
            f"{path}: [notifier.{name}] неизвестный канал; "
            f"допустимы: {sorted(_NOTIFIER_EVENT_KEYS)}"
        )
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [notifier.{name}] должна быть таблицей")
    allowed = {"events"}
    if name == "telegram":
        allowed |= _NOTIFIER_TELEGRAM_EXTRA_KEYS
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(
            f"{path}: [notifier.{name}] незнакомые ключи {sorted(unknown)}; "
            f"допустимые: {sorted(allowed)}"
        )
    if "events" not in value:
        raise ConfigError(f"{path}: [notifier.{name}] обязателен ключ events")
    extra = {
        f"notifier_telegram_{option}": value[option]
        for option in sorted(_NOTIFIER_TELEGRAM_EXTRA_KEYS & set(value))
    }
    return target, extra


def _normalize_notifier_channels(flat: dict[str, Any], path: Path) -> None:
    """Совместимость: старый ``channel = "telegram"`` равносилен ``channels``.

    Разрешение выполняется на уровне одного файла: так явное значение в
    ``robot.toml`` перекрывает вшитый дефолт, а два ключа рядом — конфликт.
    """
    if "notifier" not in flat:
        return
    legacy = flat.pop("notifier")
    if "notifier_channels" in flat:
        raise ConfigError(
            f"{path}: [notifier] задан и channels, и channel; оставьте что-то одно"
        )
    if legacy not in _ALLOWED_NOTIFIER_VALUES:
        raise ConfigError(
            f"{path}: [notifier] channel должен быть одним из "
            f"{sorted(_ALLOWED_NOTIFIER_VALUES)}, получено {legacy!r}"
        )
    get_logger(__name__).warning(
        "[notifier] channel=%r устарел: используйте channels=[%r].",
        legacy,
        legacy,
    )
    flat["notifier_channels"] = [legacy]


def _parse(path: Path, inherited: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
            if section == "notifier" and isinstance(value, dict):
                target, extra = _parse_notifier_subsection(key, value, path)
                flat[target] = value.get("events")
                flat.update(extra)
                continue
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
    _normalize_notifier_channels(flat, path)
    _merge_management_tables(flat, inherited or {}, path)
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
        result.update(_parse(path, result))
    _validate_combined_config(result, Path(config_file or CONFIG_FILENAME))
    return result
