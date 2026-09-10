## Context

Текущее состояние (мотивация — в `proposal.md`):

- `src/analysis/` совмещает «чтение рынка» (`models`, `trend`, `sr_levels`, `context_cache`) и «управление решением» (`filter`, `risk`). `risk.py` и `filter.py` импортируют `src.strategies.contracts` (`Decision`, `SignalType`) — из-за этого направление зависимости идёт из «рынка» в «стратегии».
- Стратегии — плоские файлы `src/strategies/<имя>.py`, использующие общие `src/strategies/indicators/` (подпапки индикаторов) и `src/strategies/signals.py`. Это закреплено в `strategy-contract/spec.md:112`, `indicators/directory-structure/spec.md`, `signals/spec.md`.
- `openspec/config.yaml:8` описывает стратегию как «пакет (strategy.py + indicators/ + signals/)» — противоречит спекам и коду.
- Спеки описывают поведение, а не пути пакетов: перенос/переименование компонентов не меняет ни одного требования.

## Goals / Non-Goals

**Goals:**
- Три чистых слоя с направлением зависимостей в одну сторону без циклов: `strategies` и `market_context` читают из `market_structure`; `decision` надстраивается над `strategies` и `market_context`.
- «Лакмусовое правило» для раскладки: компонент, который импортирует `strategies.contracts`, — это менеджмент решения (`diagnostic decision`), а не рынок.
- Зафиксировать имя и расположение дома под гармонические формации — `src/market_structure/` (каркас, без поведения).
- Синхронизировать `openspec/config.yaml:8` со спеками и кодом.

**Non-Goals:**
- `harmonic_abcd` и любое наполнение `market_structure/` поведением — отдельный feature-change.
- Переезд `sr_levels` на общие `swings` (устранение дубля фрактальных пивотов) — отдельный шаг позже.
- Любое изменение внешнего поведения, спеки (delta-спеки) — не создаются (`skip_specs: true`).

## Decisions

### 1. Плоский пакет `src/market_structure/`, а не вложенность или зонтик
- **Решение:** один плоский пакет `src/market_structure/`; сейчас в нём только `__init__.py` с docstring-объявлением дома. Модули `swings.py`, `fibonacci.py`, `harmonic.py` появляются в feature-change `harmonic_abcd`.
- **Почему:** «рыночная структура» — одно цельное понятие; зонтик с одним ребёнком не даёт пользы (YAGNI). Пустые модули-заглушки не создаём — это мёртвый код и обманчивая видимость работы; резервируется только пространство имён.
- **Альтернативы:** `src/market/structure/` (вложенность ради вложенности, отклонено); `src/market/` как зонтик «всё про рынок» (отклонено — сейчас пустой, и его наполнение `analysis` невозможно, см. решение 2).

### 2. `filter` + `risk` → новый пакет `src/decision/`
- **Решение:** `SignalFilter` живёт в `src/decision/filter.py`, `RiskManager` — в `src/decision/risk.py`. Импорты `src.analysis.models` внутри них меняются на `src.market_context.models`.
- **Почему:** лакмус — оба импортируют `strategies.contracts`. Они берут сигнал стратегии и меняют его (блокировка по тренду / простановка SL/TP), это «действие», а не «погода».
- **Альтернатива:** оставить `filter/risk` в `analysis` (отклонено — винегрет и зависимость «рынок→стратегии» сохраняется); положить весь `analysis` в `market/` (отклонено — `filter`/`risk` потянут `strategies` внутрь `market`, и получится кольцо `market ↔ strategies`).

### 3. Переименование `src/analysis/` → `src/market_context/`
- **Решение:** переименовать сразу вместе с выносом `decision/`.
- **Почему:** симметрия «карта + погода» с `market_structure/`; правка импортов и так происходит — два отдельных захода на те же файлы дешевле не станут.
- **Альтернатива:** оставить имя `analysis` (допустимо, но пара `market_structure` + `market_context` читается точнее; откладывание rename удвоит churn импортов).

### 4. `skip_specs: true`
- **Решение:** поведение не меняется (только расположение кода и доки-контекст) — в `.openspec.yaml` стоит `skip_specs: true`, delta-спеки не создаются.
- **Почему:** инструкция схемы: не выдумывать требования ради валидации. Спеки описывают поведение, пути пакетов в них отсутствуют.
- **Альтернатива:** впихнуть location-требования в спеки (отклонено — это «как», а не «что»).

### 5. Правка `openspec/config.yaml:8`
- **Решение:** заменить формулировку на «Стратегия = собственный файл `src/strategies/<имя>.py`, использующий общие `src/strategies/indicators/` и `src/strategies/signals.py`; регистрируется в реестре `src.strategies` через `@register`».
- **Почему:** приводит AI-контекст к фактическому стандарту (`strategy-contract`, `directory-structure`, `signals`).

### 6. Никаких изменений контрактов и внедрения зависимостей
- **Решение:** классы сохраняют конструкторы и сигнатуры; меняются только пути импорта.
- **Почему:** минимальный риск, diff сводится к переносам и `import`-правкам.

## Risks / Trade-offs

- **Объём импортного churn** (движок, entrypoint, unit-тесты, snapshot-helper, tools) → механические правки `import` строками + прогон полного `pytest`; grep по `src.analysis` (включая `.md`, `tools/`) должен дать пусто.
- **Пропущенные ссылки на старые пути** в доки/скриптах → финальная проверка `rg "src\.analysis|from src\.analysis"` по всему репозиторию.
- **Риск разрастания scope** до нерелевантного рефакторинга → фиксированный список файлов в `tasks.md`, за рамками — ничего.
- **Пустой `market_structure` может «просить» новый код на чужом месте** → docstring `__init__.py` фиксирует назначение дома; следующий change наполняет его по своему `design.md`.
- **Откат тривиален** (чистые переносы): любой конфликт решается возвратом файлов из git.

## Migration Plan

1. `git mv src/analysis/filter.py src/decision/filter.py`; `git mv src/analysis/risk.py src/decision/risk.py`; поправить внутренние импорты.
2. `git mv src/analysis src/market_context`; поправить импорты потребителей (`market_context.models`, `market_context.context_cache` и т.д.).
3. Создать `src/market_structure/__init__.py` (docstring-объявление дома).
4. Правка `openspec/config.yaml:8`.
5. `pytest` + health-проверки; `rg "src\.analysis"` = пусто.

## Open Questions

- Нет: спорные параметры гармоники (проценты фибо), наполнение `swings`/`harmonic`, кластерный объём — явно отложены в связанные изменения и не влияют на раскладку слоёв.