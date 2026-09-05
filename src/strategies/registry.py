from __future__ import annotations

import importlib
import pkgutil
from typing import TypeVar

from src.strategies.contracts import Strategy
from src.strategies.base_strategy import StrategyConfig

T = TypeVar("T", bound=Strategy)

_registry: dict[str, type[Strategy]] = {}
_packages_loaded = False


def register(cls: type[T]) -> type[T]:
    name = getattr(cls, "NAME", None)
    if not name:
        raise ValueError("У класса стратегии должен быть атрибут NAME")
    if name in _registry:
        raise ValueError(f"Стратегия '{name}' уже зарегистрирована")
    _registry[name] = cls  # type: ignore[assignment]
    return cls


def _discover_strategies() -> None:
    global _packages_loaded
    if _packages_loaded:
        return

    package = importlib.import_module("src.strategies")
    for importer, modname, ispkg in pkgutil.iter_modules(package.__path__):
        if modname.startswith("_") or modname in (
            "registry",
            "contracts",
            "base_strategy",
            "signals",
            "names",
        ):
            continue
        importlib.import_module(f"src.strategies.{modname}")

    _packages_loaded = True


def get_strategy(name: str, config: StrategyConfig) -> Strategy:
    _discover_strategies()
    if name not in _registry:
        available = ", ".join(sorted(_registry)) or "нет зарегистрированных"
        raise ValueError(f"Неизвестная стратегия '{name}'. Доступны: {available}")
    return _registry[name](config=config)


def all_strategies() -> list[Strategy]:
    _discover_strategies()
    return [cls() for cls in _registry.values()]


def strategy_names() -> list[str]:
    _discover_strategies()
    return sorted(_registry)


def validate_assignments(
    assignments: dict[str, list[str]], source: str | None = None
) -> None:
    _discover_strategies()
    unknown = sorted(
        {name for names in assignments.values() for name in names} - set(_registry)
    )
    if unknown:
        where = f" в словаре '{source}'" if source else ""
        raise ValueError(
            f"Неизвестные стратегии{where}: {', '.join(unknown)}. "
            f"Доступны: {', '.join(sorted(_registry))}"
        )
