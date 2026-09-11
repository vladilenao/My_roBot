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
import sys
import tomllib
from pathlib import Path
from typing import Any, Mapping

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
    "logging": {
        "service_uid": "logging_service_uid",
        "file": "logging_file",
        "level": "logging_level",
        "max_bytes": "logging_max_bytes",
        "backup_count": "logging_backup_count",
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
    "logging_service_uid": str,
    "logging_file": str,
    "logging_level": str,
    "logging_max_bytes": int,
    "logging_backup_count": int,
}

_ALLOWED_NOTIFIER_VALUES = {"telegram", "console"}


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
        if expected is dict:
            for name, strategies in value.items():
                if not isinstance(strategies, list) or not all(
                    isinstance(item, str) for item in strategies
                ):
                    raise ConfigError(
                        f"{path}: стратегии для {name!r} должны быть списком строк"
                    )
        cleaned[key] = value
    return cleaned


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
            target = allowed_keys.get(key)
            if target is None:
                raise ConfigError(
                    f"{path}: [{section}] незнакомый ключ {key!r}; "
                    f"допустимые: {', '.join(sorted(allowed_keys))}"
                )
            flat[target] = value
    return _validate(flat, path)


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