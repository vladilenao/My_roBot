## 1. Коды причин отказа

- [x] 1.1 Добавить в `_REJECTION_MESSAGES` (`src/trade_management/models.py`) коды `risk-budget`, `margin-committed-by-pending-orders` и `margin-budget` с человекочитаемыми описаниями на русском, различающими занятую pending-заявками маржу и нехватку общего бюджета; `risk-or-margin-budget` оставить в таблице без удаления
- [x] 1.2 Ввести узкий тип исключения отказа резервирования, несущий код отказа и числа (`used`, `candidate`, `budget`), и заменить им `raise ValueError("risk-or-margin-budget")` в `_reserve` (`src/trade_management/manager.py`)

## 2. Раздельная диагностика в `_reserve`

- [x] 2.1 Разделить проверку в `_reserve` на три независимых условия: превышен риск → `risk-budget`; превышена маржа при наличии ACTIVE-резервов → `margin-committed-by-pending-orders`; превышена маржа без ACTIVE-резервов → `margin-budget`
- [x] 2.2 Писать одну запись лога в момент отказа с числами `used_risk`, `candidate_risk`, `risk_budget`, `used_margin`, `candidate_margin`, `margin_budget`

## 3. Учёт активных резервов в sizing

- [x] 3.1 Добавить чтение сумм `risk_amount` и `margin_amount` по `reservations` со статусом `ACTIVE` для расчёта занятого бюджета
- [x] 3.2 В `_size_open_quantity` считать свободную маржу и свободный риск как бюджет за вычетом занятого ACTIVE-резервами и проверять `go * quantity` против свободной маржи вместо полного бюджета
- [x] 3.3 Сохранить нынешнюю семантику при отсутствии ACTIVE-резервов: расчётный объём и поведение одиночного входа не меняются

## 4. Проброс кода отказа вместо `admission-error`

- [x] 4.1 Обработать новое исключение резервирования в `actions_for_signal` отдельным `except` перед общим `except Exception` и вернуть `rejection_reason` с кодом отказа и числами в описании
- [x] 4.2 Убедиться, что общий `except Exception` продолжает отдавать `admission-error` для непредвиденных ошибок, и что транзакция допуска по-прежнему откатывается целиком без записей в журнале сделок

## 5. Тесты

- [x] 5.1 Тест: при ACTIVE-резерве, исчерпывающем маржу, второй вход по тому же инструменту отклоняется с кодом `margin-committed-by-pending-orders`, а не `admission-error` и не `risk-or-margin-budget`
- [x] 5.2 Тест: превышение общего бюджета маржи без ACTIVE-резервов даёт код `margin-budget`; превышение бюджета риска даёт `risk-budget`
- [x] 5.3 Тест: sizing не предлагает объём, который резервирование отклонило бы по марже (воспроизведение боевого случая: резерв 94 783 ₽, бюджет 99 380,72 ₽, второй вход 7 контрактов)
- [x] 5.4 Тест: при отказе по кодам `risk-budget`, `margin-committed-by-pending-orders`, `margin-budget` в лог записаны все шесть чисел отказа
- [x] 5.5 Тест: одиночный вход без конкуренции даёт тот же объём, что и до изменения
- [x] 5.6 Обновить существующие тесты `tests/unit/trade_management/`, ожидавшие `admission-error` или `risk-or-margin-budget` на пути резервирования

## 6. Проверка

- [x] 6.1 `.venv/bin/python -m pytest -q`
- [x] 6.2 `.venv/bin/python -m ruff check .`
- [x] 6.3 `openspec validate pending-reservation-admission --strict`
