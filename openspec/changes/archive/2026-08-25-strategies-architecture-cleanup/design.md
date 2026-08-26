## Context

Рефакторинг по Builder Pattern завершён. `src/strategies/__init__.py` содержит реестр, lazy loading и валидацию — три разные ответственности. Lazy loading хардкодит импорт `macd_rsi_stoch`. Два модуля `base.py` с разным содержимым. Функции реестра без type hints. Круговой импорт между `__init__.py` и `macd_rsi_stoch.py`.

## Goals / Non-Goals

**Goals:**
- Извлечь реестр в отдельный модуль `registry.py` (SRP)
- Заменить хардкод импортов на auto-discovery через `pkgutil.iter_modules` (OCP)
- Переименовать `base.py` → `contracts.py` для устранения двусмысленности
- Добавить type hints для всех функций реестра
- Определить `__all__` в `__init__.py`
- Убрать круговой импорт

**Non-Goals:**
- Изменение поведения стратегий или индикаторов
- Добавление новых стратегий
- Изменение snapshot-тестов

## Decisions

### 1. Реестр в `registry.py`

**Решение:** Вся логика реестра выносится в `src/strategies/registry.py`.

**Альтернативы:**
- Оставить в `__init__.py` — нарушает SRP
- Создать `registry/` пакет — избыточно для одного модуля

**Обоснование:** Один модуль, одна ответственность. `__init__.py` остаётся точкой входа пакета.

### 2. Auto-discovery через `pkgutil`

**Решение:** `_discover_strategies()` использует `pkgutil.iter_modules(src.strategies.__path__)` для обнаружения пакетов.

**Альтернативы:**
- Хардкод импортов — нарушает OCP
- Декораторы с реестром на уровне包 — сложнее, не даёт преимуществ

**Обоснование:** Стандартный механизм Python для обнаружения подмодулей. Добавление стратегии — создание пакета с `@register`, без правки реестра.

### 3. Переименование `base.py` → `contracts.py`

**Решение:** `src/strategies/base.py` переименовывается в `src/strategies/contracts.py`.

**Альтернативы:**
- Оставить `base.py` — источник путаницы с `indicators/base.py`
- `types.py` — неконкретно, содержит Protocol, а не просто типы

**Обоснование:** `contracts.py` точно описывает содержимое: `Decision`, `SignalType`, `Strategy` Protocol.

### 4. Круговой импорт

**Решение:** `registry.py` не импортирует конкретные стратегии. `macd_rsi_stoch.py` импортирует `register` из `registry.py`. Нет цикла.

**Альтернативы:**
- Lazy import внутри функций — хрупко
- Runtime registration — сложнее

**Обоснование:** Прямой импорт `registry` из `macd_rsi_stoch` без обратного импорта.

## Risks / Trade-offs

[Auto-discovery может найти лишние модули] → Фильтрация по `__init__.py` и отсутствию префикса `_`

[Изменение пути импорта `base.py` → `contracts.py`] → Mass-update импортов во всех файлах, затронутых в Impact

[Type hints могут сломать существующий код] → Проверка через `mypy` и тесты
