"""Реестр профилей фильтрации: имя профиля → экземпляр фильтра.

Новый профиль = новый класс + запись в ``PROFILES``; главный цикл робота
не меняется (фабричный выбор по имени инкапсулирован в ``SignalFilter``).
"""

from src.decision.filters.base import ProfileFilter
from src.decision.filters.basic_levels import BasicLevelsFilter
from src.decision.filters.null import NullFilter

PROFILES: dict[str, ProfileFilter] = {
    "raw": NullFilter(),
    "basic_levels": BasicLevelsFilter(),
}


def profile_names() -> list[str]:
    """Отсортированный список доступных имён профилей фильтрации."""
    return sorted(PROFILES)
