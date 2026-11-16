Реализация выполнена. Единственная отложенная проверка — пункт 4.1: владелец проводит её после подключения UI (решение 03.11.2026).

## 1. Данные

- [x] 1.1 `src/broker_contour/bindings.py`: `binding_brief(card)` — `id`, `strategy`, `timeframe`, `filter`, `management`; `instrument_bindings(card, catalog)` — привязки участника эмуляции по правилам `assignments_for`.
- [x] 1.2 `SandboxControl`: `filter` и `management` у участника (из каталога, иначе из карточки состава) и в `available`.
- [x] 1.3 `LaunchControl`: принимает `share_strategies`/`future_strategies`; `bindings` у участника эмуляции, в том числе до записи состава (по выбранным инструментам). `run.py` передаёт привязки конфигурации.

## 2. Контракт и документы

- [x] 2.1 `docs/api/openapi.yaml` 2.5.0: `filter`, `management` в `BindingCard` и `BindingMember`; схема `MemberBinding` и поле `bindings` у `Member`.
- [x] 2.2 `docs/api/README.md` — что нового в 2.5.0.

## 3. Тесты

- [x] 3.1 Песочница: `filter`/`management` у участника и в каталоге, `raw` отдаётся значением; привязка, которой нет в конфигурации, — из карточки состава.
- [x] 3.2 Эмуляция: `bindings` у фьючерса по семейству, у акции по тикеру, пустой список без привязок; ответы проходят схемы Swagger.
- [x] 3.3 Полный `pytest` и `ruff check`.

## 4. Ручная проверка владельцем

- [ ] 4.1 Тестовый робот (8102): `GET /api/v1/sandbox` и `GET /api/v1/emulation` — у привязок BR-12.26 и ED-12.26 фильтр и управление совпадают с `robot.toml`. **Отложено (03.11.2026):** владелец проверит после подключения полей в UI.
