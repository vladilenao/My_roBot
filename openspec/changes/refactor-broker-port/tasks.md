## 1. Порт и адаптер Т-Инвест

- [ ] 1.1 `src/broker_contour/port.py`:
  - протоколы `BrokerApi`, `BrokerAccess`;
  - свойства `BrokerTraits` и профили `TINVEST_SANDBOX`, `TINVEST_ACCOUNT`;
  - отчёты, `OrderFailure`, `BrokerCallError`, `AccountRuleError`, `RobotAccount`, `AccountSnapshot`, `mask_account`.
- [ ] 1.2 Пакет `src/broker_contour/tinvest/`: `api.py`, `gateway.py`, `account.py`, `admission.py`, `access.py`. Реэкспорты в `src/sandbox/gateway.py` и `src/sandbox/account.py`. Удалить `broker_contour/api.py`, `broker_contour/account.py`, `account/account.py`, `account/admission.py`; импорты кода и тестов — на новые места.
- [ ] 1.3 `TInvestBroker` → `LiveBroker`, алиас `TInvestBroker`. Свойства вместо констант и условий:
  - префикс исполнений в `broker.py` и `fees.py`;
  - `rub_amounts` в `stops.py`;
  - `max_lots` вместо `contour_id == "account"`;
  - `margin_every`, `order_lookup`, `stop_lookup`, `fees`.
- [ ] 1.4 `process.py` и `admission.py` работают через `BrokerAccess`: снимок, капитал, имена, правило счёта, факты допуска.

## 2. Описание контура

- [ ] 2.1 `src/broker_contour/contours.py`: `LiveContour`, `LIVE_CONTOURS` (`account`, `sandbox`), чтение `[contours.<id>]` по номеру.
- [ ] 2.2 `run.py`: одна функция запуска контура на брокере по описанию; надзиратели циклом по описаниям; `BrokerContourSupervisor(contour_id, kind)`; `BrokerContourSettings.kind` в `status.json`; заголовки карточек из описания.

## 3. Тесты

- [ ] 3.1 Существующие тесты `tests/unit/` проходят. Меняются только импорты и необязательные параметры фейков, проверки — нет.
- [ ] 3.2 `tests/unit/broker_contour/test_broker_port.py`:
  - общий код не импортирует `t_tech`, `grpc`, `src.sandbox` (AST, и внутри функций);
  - `TInvestApi` и фейк удовлетворяют `BrokerApi`;
  - контрактный сценарий: вход, цель, стоп у брокера, срабатывание, рестарт, досверка — на фейке Т-Инвест;
  - ветки `order_lookup = number` и `stop_lookup = key` на фейке.
- [ ] 3.3 Старая база: `broker.sqlite3` и `trades.sqlite3` с исполнениями `tinvest:` и `tinvest-order:` → рестарт, повтор тех же сделок брокером — без новых исполнений; `FeeSync` находит ждущие комиссии.
- [ ] 3.4 Описание контура:
  - `status.json` у `sandbox` и `account` с прежними `contour_id` и `kind`;
  - `launch.json` прежний;
  - заголовки «ПЕСОЧНИЦА» и «СЧЁТ»;
  - «Счёт» поднимается первым.
- [ ] 3.5 Полный `pytest` и `ruff check src tests run.py`.
- [ ] 3.6 Сборка PyInstaller `run.spec` проходит; собранный `--contour sandbox` на тестовых данных доходит до допуска без `ImportError`.

## 4. Ручной регресс владельца в песочнице Т-Инвест (до ПРОМ)

Тестовый робот из PyCharm на 8102. ПРОМ (8101, `~/Desktop/my_roBot_app`) не трогать.

- [ ] 4.1 Рестарт с открытой позицией: стоп у брокера не задваивается, исполнения за простой проводятся.
- [ ] 4.2 Сверка и уточнение комиссий.
- [ ] 4.3 Предохранители: срабатывание и сброс.
- [ ] 4.4 `close_all`.
- [ ] 4.5 Сравнение песочницы с эмуляцией.
- [ ] 4.6 «Счёт» на чтение (`orders = false`) стартует и сверяется.
