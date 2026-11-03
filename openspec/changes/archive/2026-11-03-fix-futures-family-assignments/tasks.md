## 1. Исправление

- [x] 1.1 `src/instruments/futures.py`: `assignment_family(ticker, short_name, family)` — семейство карточки, иначе `family_for`; неоднозначность — `None`.
- [x] 1.2 `src/bot/trading_bot.py` `assignments_for` и `src/history/preflight.py` `instrument_assignments` — поиск по `assignment_family`.

## 2. Тесты

- [x] 2.1 RIZ6, SRZ6, GDZ6, SVZ6 без семейства находят привязки `RTS`, `SBRF`, `GOLD`, `SILV`; код семейства (`SR`) как ключ конфигурации; семейство из карточки; акция по тикеру; неизвестный фьючерс — пусто.
- [x] 2.2 Предпроверка истории — то же для `instrument_assignments`.
- [x] 2.3 Полный `pytest` и `ruff check`.

## 3. Проверка владельцем

- [ ] 3.1 Тестовый робот: после перезапуска эмуляции ядро держит для RIZ6, SRZ6, GDZ6, SVZ6 таймфреймы их привязок, `GET /api/v1/emulation` (после вливания в `feature/contour-trades-api`) показывает их `bindings`. **Владелец проверяет после вливания в feature/contour-trades-api и перезапуска тестового робота.**
