from __future__ import annotations

from collections.abc import Callable

from src.market_context.models import MarketContext, SRLevel, TrendResult


class MarketContextError(RuntimeError):
    """Сбой ленивого расчёта контекста.

    Расчёт теперь идёт внутри анализа стратегий и допуска, где ``ValueError``,
    ``TypeError`` и ``KeyError`` считаются проблемой стратегии и гасятся
    предупреждением. Сбой контекста — сбой тика, поэтому он отдельного типа.
    """


class LazyMarketContext:
    """Рыночный контекст, который вычисляется при первом чтении поля.

    Тик запрашивает контекст для каждого готового таймфрейма, но читают его
    только фильтры входа и сопровождение. Без чтения тренд и S/R не считаются;
    после первого чтения значение запоминается и не пересчитывается.
    """

    __slots__ = ("_load", "_on_load", "_value")

    def __init__(
        self,
        load: Callable[[], MarketContext],
        on_load: Callable[[MarketContext], None] | None = None,
    ) -> None:
        self._load = load
        self._on_load = on_load
        self._value: MarketContext | None = None

    @property
    def loaded(self) -> bool:
        return self._value is not None

    def resolve(self) -> MarketContext:
        if self._value is None:
            try:
                self._value = self._load()
            except Exception as exc:
                raise MarketContextError(f"рыночный контекст не вычислен: {type(exc).__name__}") from exc
            if self._on_load is not None:
                self._on_load(self._value)
        return self._value

    @property
    def trend(self) -> TrendResult:
        return self.resolve().trend

    @property
    def sr_levels(self) -> list[SRLevel]:
        return self.resolve().sr_levels

    @property
    def current_price(self) -> float:
        return self.resolve().current_price

    def __repr__(self) -> str:
        if self._value is None:
            return "LazyMarketContext(не вычислен)"
        return repr(self._value)
