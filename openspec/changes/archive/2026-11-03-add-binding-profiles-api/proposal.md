## Why

В UI робота таблица «Инструменты» контура показывает у каждой привязки стратегию, таймфрейм и `id`. Владелец 03.11.2026 решила показывать там же **фильтр** и **управление** привязки: теги «Старшие ТФ», «По тренду», зачёркнутая воронка для `raw` и тег управления со значком цели. Без раскрытия строки.

Робот это знает — `filter` и `management` есть у каждой привязки `[strategies.*]` и в карточке участника песочницы, — но API их не отдаёт:

- у песочницы `BindingMember` и `BindingCard` — только инструмент, стратегия, таймфрейм;
- у эмуляции `Member` вообще без привязок: UI сопоставляет каталог `GET /api/v1/sandbox/bindings` по семейству (пробел № 9 в `UI/docs/api/gaps.md`), и это догадка UI, а не данные робота.

## What Changes

- **Песочница.** `BindingMember` (`GET /api/v1/sandbox`, `…/bindings` → `members`) и `BindingCard` (`…/bindings` → `available`) получают `filter` и `management` — профили из текущей конфигурации.
- **Эмуляция.** `Member` (`GET /api/v1/emulation` → `instruments`, `…/instruments`, `…/instruments/{name}`) получает `bindings` — привязки конфигурации, по которым торгует инструмент: `id`, `strategy`, `timeframe`, `filter`, `management`. Подбор — по тем же правилам, что у торговли (`assignments_for`): акция по тикеру, фьючерс по семейству или его коду. Закрывает пробел № 9.
- **Значения — как в конфигурации**, без перевода: `filter` — `raw`, `basic_levels`, `triple_screen`; `management` — `levels_rr`, `atr_trend`, `ma_cloud`, `pattern_targets`. Подписи («Старшие ТФ», «По тренду», …) — дело UI. Перечисления в схеме не закрываются: новый профиль не ломает клиента.
- **Swagger** `docs/api/openapi.yaml` — 2.5.0, изменения только добавляют поля.

## Capabilities

### Modified Capabilities

- `control-api` — новое требование «Профили привязки в ответах контуров».

## Impact

- `src/broker_contour/bindings.py` — привязки участника эмуляции и краткая карточка привязки.
- `src/bot/sandbox_control.py`, `src/bot/launch_control.py`, `run.py` — поля в ответах; `LaunchControl` получает привязки конфигурации.
- `docs/api/openapi.yaml`, `docs/api/README.md`; тесты `tests/unit/bot/test_sandbox_api.py`, `tests/unit/bot/test_launch_control.py`.
- UI: `BindingStrategy`, `InstrumentTable`, `lib/strategies.ts` — отдельно, в репозитории `UI`.
