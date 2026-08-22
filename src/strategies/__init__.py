_registry = {}


def register(cls):
    name = getattr(cls, "NAME", None)
    if not name:
        raise ValueError("У класса стратегии должен быть атрибут NAME")
    if name in _registry:
        raise ValueError(f"Стратегия '{name}' уже зарегистрирована")
    _registry[name] = cls
    return cls


def get_strategy(name):
    if name not in _registry:
        available = ", ".join(sorted(_registry)) or "нет зарегистрированных"
        raise ValueError(f"Неизвестная стратегия '{name}'. Доступны: {available}")
    return _registry[name]()


def all_strategies():
    return [cls() for cls in _registry.values()]


from src.strategies.macd_rsi_stoch import MacdRsiStochStrategy  # noqa: E402,F401
