## 1. Настройки

- [ ] 1.1 `[contours.account]`: `orders = true` принимается; ключи `exit_offset_steps`, `exit_requote_secs`, `exit_requote_attempts`, `stop_place_secs`, `max_daily_loss_pct`, `max_rejects_in_row` с диапазонами и значениями песочницы в `config_loader.py`, `config.py`, `default.toml` (`orders = false` по умолчанию остаётся).

## 2. Процесс «Счёта» с заявками

- [ ] 2.1 `run.py` `_run_account_contour`: при `orders = true` — `ExitPolicy`, пороги предохранителей, фабрика торговой части `_broker_trading_factory("account", …)`, каналы с торговыми событиями (design.md решение 1).
- [ ] 2.2 Ядро: спрос свечей и карточки контрактов привязок «Счёта» в `launch.json` — как у песочницы (`resolve_binding` в `_account_control`).
- [ ] 2.3 Юнит-тесты: процесс «Счёта» с `orders = true` поднимает торговую часть на шлюзе счёта; с `orders = false` — без неё, как на Р1.

## 3. API «Счёта»

- [ ] 3.1 `AccountControl`: режим заявок из настроек; при `orders = true` — запуск с `bindings`, добавление и снятие привязок; проверка «привязка в составе песочницы» → `422 BINDINGS_REJECTED` (решение 3); при `orders = false` — `409 ACCOUNT_READ_ONLY`.
- [ ] 3.2 Подтверждение только у `close_all`: `PUT …/target` и `POST …/bindings/remove` с `mode = close_all` без `confirm: true` — `409 CONFIRM_REQUIRED`; `confirm` не логического типа — `400` (решение 4).
- [ ] 3.3 `POST /api/v1/account/guards/reset` и `_actions` «Счёта»: добавить привязки, снять предохранитель, остановки (решение 5).
- [ ] 3.4 Юнит-тесты API: состав из песочницы и не из неё, режим чтения, `CONFIRM_REQUIRED` у обоих `close_all`, `no_entries` без подтверждения, сброс предохранителя, ответ по схеме OpenAPI.

## 4. Сравнение трёх контуров

- [ ] 4.1 `src/bot/comparison_view.py`: обобщить на список сторон с опорной первой; отчёт песочницы — две стороны, ответ не меняется (решение 7).
- [ ] 4.2 `GET /api/v1/account/comparison`: стороны `account`, `sandbox`, `emulation`, расхождения `account_vs_sandbox` и `account_vs_emulation`, `summary` по сторонам; маршрут в `export_api.py`.
- [ ] 4.3 Юнит-тесты: сделка во всех трёх, отказ бюджета на счёте, нет журнала счёта, отчёт песочницы без изменений.

## 5. Документы и прогон

- [ ] 5.1 `docs/api/openapi.yaml` 2.12.0: маршруты `comparison` и `guards/reset` «Счёта», состав и `confirm` у `bindings/remove`, схемы `AccountComparison` и `AccountBindingsRemoveRequest`.
- [ ] 5.2 Полный `pytest` и `ruff check src tests run.py`.

## 6. Ручная проверка владельцем

- [ ] 6.1 `[contours.account] orders = true`, перезапуск «Счёта»: в карточке старта «Режим: заявки к брокеру», токен с полным доступом, предупреждений нет.
- [ ] 6.2 Добавить привязку не из песочницы — `422` с причиной; привязку из песочницы — `202`, она в составе «Счёта».
- [ ] 6.3 Первая сделка на счёте: лимитная заявка входа, после исполнения одна стоп-заявка на весь объём у брокера, цели, карточки в канале «Счёт»; через 15 минут у исполнений комиссия брокера (`fees_source = broker`).
- [ ] 6.4 `close_all` без `confirm` — `409`; с `confirm: true` — стоп снят, позиция закрыта, на счёте пусто.
- [ ] 6.5 `GET /api/v1/account/comparison` — сделка счёта рядом с песочницей и эмуляцией, расхождения посчитаны.
- [ ] 6.6 Две недели без ручного вмешательства и без расхождений (документ 2) — срок решает владелец.
