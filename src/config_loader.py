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
    },
    "trading": {
        "initial_deposit": "initial_deposit",
        "max_risk_pct": "max_risk_pct",
        "journal_file": "journal_file",
        "clearing_times": "clearing_times",
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
    "clearing_times": list,
}

_ALLOWED_NOTIFIER_VALUES = {"telegram", "console"}

# Таблица тикера в [strategies.*]: гибридный массив `strategies` (строка с именем
# стратегии | инлайн-таблица {name, filter, tf}) + опциональный базовый `timeframe`.
_STRATEGY_TABLE_KEYS = {"strategies", "timeframe"}
_STRATEGY_ENTRY_KEYS = {"name", "filter", "tf"}


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
    """Проверка одного элемента гибридного массива `strategies` таблицы тикера."""
    if isinstance(item, str):
        if not item:
            raise ConfigError(
                f"{path}: пустое имя стратегии в привязке для {ticker!r}"
            )
        return
    if not isinstance(item, dict):
        raise ConfigError(
            f"{path}: элемент strategies для {ticker!r} должен быть строкой "
            f"или инлайн-таблицей {{name, filter}}, получено {type(item).__name__}"
        )
    unknown = set(item) - _STRATEGY_ENTRY_KEYS
    if unknown:
        raise ConfigError(
            f"{path}: привязка стратегии для {ticker!r}: незнакомые ключи "
            f"{sorted(unknown)}; допустимые: {sorted(_STRATEGY_ENTRY_KEYS)}"
        )
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(
            f"{path}: привязка стратегии для {ticker!r} требует непустой "
            f"строковый ключ 'name'"
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
    return result