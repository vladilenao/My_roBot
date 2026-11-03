## Why

03.11.2026 UI показал у участников эмуляции RTS-12.26, SBRF-12.26, GOLD-12.26 и SILV-12.26 пустой список привязок. Разбор на тестовом роботе показал баг торговли, а не только API:

- карточки состава контуров (`contour_members`, `launch.json`) хранят у фьючерсов `family = null`;
- `assignments_for` (торговля, спрос ядра на таймфреймы) и `instrument_assignments` (предпроверка истории) без семейства ищут привязки по двум буквам тикера: RIZ6 → `RI`, SRZ6 → `SR`, GDZ6 → `GD`, SVZ6 → `SV`;
- привязки в `robot.toml` записаны под `RTS`, `SBRF`, `GOLD`, `SILV` — их не находит никто. У NG, BR, Si, ED две буквы совпадают с ключом, поэтому они торгуют.

Итог: ядро держит для RIZ6, SRZ6, GDZ6, SVZ6 только `1m`, в журнале эмуляции нет ни одной сделки по `future-rts-*`, `future-sbrf-*`, `future-gold-*`, `future-silv-*` с добавления 25.10. Та же логика — в `release/release-11`, то есть, по коду, и на ПРОМ v8.0.0.

## What Changes

- **Семейство для поиска привязок.** Новая `assignment_family(ticker, short_name, family)` в `src/instruments/futures.py`: семейство из карточки, а если его нет — по тикеру и короткому имени через существующую `family_for` (`RIZ6` / `RTS-12.26` → `RTS`). Неоднозначная карточка — `None` и прежний запасной путь по двум буквам.
- `assignments_for` и `instrument_assignments` ищут привязки фьючерса по этому семейству: сначала ключ семейства, потом его код, как раньше.
- **Поведение торговли меняется**: после обновления эмуляция начинает торговать RTS, SBRF, GOLD, SILV по их привязкам, ядро — грузить их таймфреймы. Решение владельца 03.11.2026: «Торговля и API».
- Карточки состава и формат `contour_members` не меняются: семейство определяется при поиске, миграция не нужна.

## Capabilities

### Modified Capabilities

- `instruments` — новое требование «Привязки фьючерса по семейству контракта».

## Impact

- `src/instruments/futures.py`, `src/bot/trading_bot.py`, `src/history/preflight.py`.
- Тесты: `tests/unit/bot/test_assignment_family.py`.
- После вливания в `feature/contour-trades-api` той же функцией пользуется `instrument_bindings` (API 2.5.0, `bindings` участника эмуляции).
