from importlib import import_module as _import_module

_registry = {}
_packages_loaded = False


def register(cls):
    name = getattr(cls, "NAME", None)
    if not name:
        raise ValueError("У класса стратегии должен быть атрибут NAME")
    if name in _registry:
        raise ValueError(f"Стратегия '{name}' уже зарегистрирована")
    _registry[name] = cls
    return cls


def _ensure_registered():
    global _packages_loaded
    if _packages_loaded:
        return
    _import_module("src.strategies.macd_rsi_stoch")
    _packages_loaded = True


def get_strategy(name):
    _ensure_registered()
    if name not in _registry:
        available = ", ".join(sorted(_registry)) or "нет зарегистрированных"
        raise ValueError(f"Неизвестная стратегия '{name}'. Доступны: {available}")
    return _registry[name]()


def all_strategies():
    _ensure_registered()
    return [cls() for cls in _registry.values()]


def strategy_names():
    _ensure_registered()
    return sorted(_registry)


def validate_assignments(assignments):
    _ensure_registered()
    unknown = sorted(
        {name for names in assignments.values() for name in names} - set(_registry)
    )
    if unknown:
        raise ValueError(
            f"Неизвестные стратегии в привязках: {', '.join(unknown)}. "
            f"Доступны: {', '.join(sorted(_registry))}"
        )
