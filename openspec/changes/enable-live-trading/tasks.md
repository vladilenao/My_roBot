## 1. Настройки

- [x] 1.1 `[contours.account]`: `orders = true` принимается; ключи `exit_offset_steps`, `exit_requote_secs`, `exit_requote_attempts`, `stop_place_secs`, `max_daily_loss_pct`, `max_rejects_in_row` с диапазонами и значениями песочницы в `config_loader.py`, `config.py`, `default.toml` (`orders = false` по умолчанию остаётся).

## 2. Процесс «Счёта» с заявками

- [x] 2.1 `run.py` `_run_account_contour`: при `orders = true` — `ExitPolicy`, пороги предохранителей, фабрика торговой части `_broker_trading_factory("account", …)`, каналы с торговыми событиями (design.md решение 1).
- [x] 2.2 Ядро: спрос свечей и карточки контрактов привязок «Счёта» в `launch.json` — как у песочницы (`resolve_binding` в `_account_control`).
- [x] 2.3 Юнит-тесты: процесс «Счёта» с `orders = true` поднимает торговую часть на шлюзе счёта; с `orders = false` — без неё, как на Р1.

## 3. API «Счёта»

- [x] 3.1 `AccountControl`: режим заявок из настроек; при `orders = true` — запуск с `bindings`, добавление и снятие привязок; проверка «привязка в составе песочницы» → `422 BINDINGS_REJECTED` (решение 3); при `orders = false` — `409 ACCOUNT_READ_ONLY`.
- [x] 3.2 Подтверждение только у `close_all`: `PUT …/target` и `POST …/bindings/remove` с `mode = close_all` без `confirm: true` — `409 CONFIRM_REQUIRED`; `confirm` не логического типа — `400` (решение 4).
- [x] 3.3 `POST /api/v1/account/guards/reset` и `_actions` «Счёта»: добавить привязки, снять предохранитель, остановки (решение 5).
- [x] 3.4 Юнит-тесты API: состав из песочницы и не из неё, режим чтения, `CONFIRM_REQUIRED` у обоих `close_all`, `no_entries` без подтверждения, сброс предохранителя, ответ по схеме OpenAPI.

## 4. Сравнение трёх контуров

- [x] 4.1 `src/bot/comparison_view.py`: обобщить на список сторон с опорной первой; отчёт песочницы — две стороны, ответ не меняется (решение 7).
- [x] 4.2 `GET /api/v1/account/comparison`: стороны `account`, `sandbox`, `emulation`, расхождения `account_vs_sandbox` и `account_vs_emulation`, `summary` по сторонам; маршрут в `export_api.py`.
- [x] 4.3 Юнит-тесты: сделка во всех трёх, отказ бюджета на счёте, нет журнала счёта, отчёт песочницы без изменений.

## 5. Документы и прогон

- [x] 5.1 `docs/api/openapi.yaml` 2.12.0: маршруты `comparison` и `guards/reset` «Счёта», состав и `confirm` у `bindings/remove`, схемы `AccountComparison` и `AccountBindingsRemoveRequest`.
- [x] 5.2 Полный `pytest` и `ruff check src tests run.py`.

  Сделано 16.11: настройки — `config_loader.py`, `config.py`, `default.toml` (диапазоны общие с песочницей); `run.py` — `_run_account_contour` с `ExitPolicy`, порогами и торговой частью, разведка комиссий только при `orders = false`; `_account_control` с режимом, составом песочницы и `resolve_binding`, `_account_comparison`; `BrokerContour._serve_trading` пишет факты допуска и причину первой тревоги; `src/bot/account_control.py` (`parse_account_target`, `parse_account_bindings`, `put_account_target`, `change_account_bindings`); `src/bot/comparison_view.py` — `_diffs_between`, `AccountComparison` (отчёт песочницы не изменён); `export_api.py` — `guards/reset`, `comparison`; `docs/api/openapi.yaml` 2.12.0. Тесты: `test_account_api.py` (+5, обновлены 3 теста Р1), `test_contour_comparison.py` (+3), `test_account_contour.py` (+1), `test_contour_config.py` (+3). Полный `pytest` 4175 passed, `ruff check` чистый.

- [x] 5.3 Комиссия брокера на счёте: окно `GetOperationsByCursor` назад — сутки, а не минута; операция счёта датирована постановкой заявки (найдено на проверке 6.3, 17.11: вход ждал 85 с, комиссия 20,76 ₽ не подтянулась). Тест `test_operation_dated_at_order_placement_hours_before_fill`.
- [x] 5.4 Объём входа и добора на счёте не больше `GetMaxLots` брокера (`limiting_constraint`/отказ `broker-margin`); тревога «отказы подряд» на счёте называет `POST /api/v1/account/guards/reset` (найдено 18.11: вход BR-12.26 на 3 контракта трижды отклонён `30042 Not enough assets for a margin trade` — ГО биржи 3 × 16 432 = 49 296 ₽ при капитале 49 825 ₽, а брокер давал 2; третий отказ включил предохранитель, он отклонил входы GOLD и SILV). `TInvestApi.max_lots`, `TInvestBroker.entry_lots_limit` (только `ask_max_lots` у «Счёта»), `TradeManager._broker_lots_limit` в `_size_open_quantity` и `_size_addition`. Тесты в `test_portfolio_budget_v2.py` (+4), `test_tinvest_broker.py` (+2), `test_account_guards.py`.

## 6. Ручная проверка владельцем

- [x] 6.1 `[contours.account] orders = true`, перезапуск «Счёта»: в карточке старта «Режим: заявки к брокеру», токен с полным доступом, предупреждений нет.
  Проверено 17.11: «Счёт» работает с `orders = true` (`orders_enabled = true`), токен с полным доступом, предупреждений нет; привязки Si и ED в составе.
- [x] 6.2 Добавить привязку не из песочницы — `422` с причиной; привязку из песочницы — `202`, она в составе «Счёта».
  Проверено владельцем 17.11: привязка TCSG (нет в песочнице) — `422` с причиной; привязки Si и ED из песочницы — в составе «Счёта».
- [x] 6.3 Первая сделка на счёте: лимитная заявка входа, после исполнения одна стоп-заявка на весь объём у брокера, цели, карточки в канале «Счёт»; через 15 минут у исполнений комиссия брокера (`fees_source = broker`).
  Проверено 17.11 на счёте …7029: сигнал `future-si-macd` 13:00, лимит продажи 1 Si-12.26 по 83 054 принят и исполнен в 13:01:32; через секунду одна стоп-заявка на 1 лот по 83 619, цель — лимит покупки 81 920 у брокера; карточки в канале «Счёт»; сверка `ok`. Комиссия брокера 20,76 ₽ (`fees_source = broker`) — после правки 5.3 и перезапуска 13:36; после перезапуска стоп-заявка та же, дубля нет. До правки за 23 минуты комиссия оставалась оценкой 1,50 ₽.
- [ ] 6.4 `close_all` без `confirm` — `409`; с `confirm: true` — стоп снят, позиция закрыта, на счёте пусто.
- [ ] 6.5 `GET /api/v1/account/comparison` — сделка счёта рядом с песочницей и эмуляцией, расхождения посчитаны.
- [ ] 6.6 Две недели без ручного вмешательства и без расхождений (документ 2) — срок решает владелец.
