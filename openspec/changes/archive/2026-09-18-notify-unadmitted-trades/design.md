## Context

См. proposal.md — Why (молчаливый недопуск сигналов в торговом режиме). Ключевые точки кода:

- `TradeManager.actions_for_signal` (`src/trade_management/manager.py:227-254`) возвращает `tuple[TradeAction, ...]`; молчаливый возврат пустоты происходит в ветках: `meta is None` (243-245), исключение (252-254), `_plan_entry` — неизвестный профиль (372-374), `ProfileResult`/`None`-план (400-401), `quantity <= 0` (402-404), `submit_plan` = False (445).
- Оркестратор: `_admit_candidate` (`src/bot/trading_bot.py:329-348`) вызывает `actions_for_signal` и передаёт результат в `_dispatch_management_actions`, который просто итерирует действия; причины недопуска никак не доносятся.
- Доставка сообщений идёт через порт исполнения → нотификатор (`NotifyOnlyExecutionPort.execute` → `notifier.notify_decision`), форматтер — `DecisionFormatter` (`src/notifier/base.py`).

## Goals / Non-Goals

**Goals:**
- Дать `actions_for_signal` структурированный результат допуска с причинами недопуска (код + русский текст), не меняя порядок допуска и синхронного сопровождения.
- Доносить каждую причину недопуска по BUY/SELL-кандидату в консоль/Telegram через существующий слой порт → нотификатор.
- Сохранить прежний формат обычных сигналов без изменений.

**Non-Goals:**
- Не менять логику допуска, риск-размера или сопровождения открытых сделок.
- Не выводить причины недопуска добора `AddToTrade` и фильтрованного HOLD сопровождения (`_manage_owned`): это ветка сопровождения, а не входного допуска.
- Не вводить дедупликацию/rate-limiting сообщений о недопуске.
- Не писать причины недопуска в журнал сделок/БД (это уже покрыто calculation-trace для планирующих профилей).

## Decisions

### 1. Результат допуска — dataclass `SignalAdmission` вместо кортежа

В `src/trade_management/models.py` добавить:

```python
@dataclass(frozen=True)
class RejectionReason:
    code: str      # машиночитаемый код (no-contract-metadata, unknown-profile, zero-quantity, duplicate-signal, admission-error, или код профиля)
    message: str   # человекочитаемый текст на русском

@dataclass(frozen=True)
class SignalAdmission:
    actions: tuple[TradeAction, ...] = ()
    rejections: tuple[RejectionReason, ...] = ()
    def __iter__(self): return iter(self.actions)   # совместимость с итерацией действий
```

- `actions_for_signal` возвращает `SignalAdmission`; оркестратор (`_admit_candidate`) читает `.actions` для исполнения и `.rejections` для уведомлений.
- Итерация по самому объёкту отдаёт действия: существующие циклы `for action in actions or ()` и проверки `len(admission)` остаются корректными.
- Обоснование: кортеж не мог нести причины; отдельный второй метод (например `rejections_for_signal`) потребовал бы повторного пересчёта допуска и расходился бы с результатом при гонках. Альтернатива «передавать mutable-лист причин параметром» отклонена: ломает чистоту публичного API менеджера и тесты.
- BREAKING для внутренних вызовов: моковые значения `actions_for_signal.return_value = ()` в тестах нужно обновить на `SignalAdmission()` (см. tasks).

### 2. Сбор причин на месте каждого молчаливого отказа

В `actions_for_signal` и `_plan_entry` каждая ветка `return ()` заменяется на возврат `SignalAdmission` с соответствующей причиной:

- `meta is None` → `no-contract-metadata`, текст «Нет метаданных контракта для инструмента»;
- исключение → `admission-error`, текст из исключения;
- `_plan_entry`: неизвестный профиль → `unknown-profile`, «Неизвестный профиль управления <имя>»;
- `ProfileResult` (план = отказ) → код из `plan.state["reason"]`, человекочитаемый текст по таблице известных кодов (`insufficient-history`, `missing-structure`, `missing-pattern-context`, `target-not-ahead`), fallback — сырой код;
- `quantity <= 0` → `zero-quantity`, «Размер позиции ниже минимального»;
- `submit_plan` = False → `duplicate-signal`, «Сигнал уже обработан ранее».

HOLD не даёт причин: возвращается пустой `SignalAdmission()` — штатное отсутствие сигнала, не недопуск. Таблица код → текст живёт рядом с `RejectionReason` (модуль-константа или функция), чтобы не раздувать менеджер.

### 3. Формат и доставка уведомления о недопуске

Расширить слой «порт → нотификатор» без изменения формата обычных сигналов:

- `DecisionFormatter.format_rejection(...)` — строит заголовок тем же способом, что `format` (`●` → `⛔`), сигнальная часть `➜ Сделка не допущена: <причина>`. Блоки таймфрейма/времени/стратегии/профиля включаются по наличию, как у обычных сигналов.
- `AbstractNotifier.notify_rejection(decision, instrument_label, *, reason_message, timeframe="")` — по аналогии с `notify_decision`: форматирует через `format_rejection` и доставляет через `notify`. Telegram и консольный нотификаторы наследуют реализацию, менять их не требуется.
- `ExecutionPort` получает абстрактный `report_rejection(decision, instrument, *, reason_message, timeframe="")`; `NotifyOnlyExecutionPort` → `notifier.notify_rejection` с коротким именем инструмента; `BrokerExecutionPort` → уведомление при наличии нотификатора (зеркально поведению `execute`).
- Оркестратор в `_admit_candidate`: после исполнения действий для каждой `reason in admission.rejections` вызывает `self._execution.report_rejection(...)` с контекстом задачи (инструмент, таймфрейм, решение). Тексты не собирает сам — только передаёт причину.

Обоснование: сохранена единая точка вывода (порт) и внедрённый форматтер; не дублируется формирование заголовка. Альтернатива «печатать напрямую `print` в консоль» отклонена — обходила бы нотификатор/Telegram и нарушала иерархию.

### 4. Тесты

- `tests/unit/trade_management/test_signal_pipeline.py`: обновить вызовы под `SignalAdmission`; добавить кейсы причин (нет метаданных, дубликат, нулевой объём, отказ профиля).
- `tests/unit/bot/test_trading_bot.py`: обновить моки `actions_for_signal.return_value`; `RecordingExecution` дополнить `report_rejection`; новый тест доставки причины недопуска с контекстом задачи и тест «HOLD не даёт причины».
- `tests/unit/execution/test_port.py`: тесты `report_rejection` для обоих портов.
- `tests/unit/notifier/*`: тесты `notify_rejection`/`format_rejection` (маркер `⛔`, включение/опускание блоков).

## Risks / Trade-offs

- [Шум в консоли при повторяющемся недопуске одного и того же сигнала на каждом тике] → По контракту уведомление о недопуске повторяется вместе с сигналом; дедупликация осознанно вне рамок. Если станет мешать — отдельный change с подавлением по (код, instrument, time) за окно.
- [BREAKING: смена типа возврата `actions_for_signal`] → `SignalAdmission` итерируется как кортеж действий; единственные ломающиеся места — моки в тестах, обновляются в этом change.
- [Telegram: недопуски уходят трейдеру как сообщения] → Соответствует контракту нотификатора; канал выбирается как для обычных сигналов. Объём ограничен числом недопущенных кандидатов.
- [`_manage_owned` не покрыт причинами] → Осознанно: добор/фильтрованный HOLD — сопровождение, а не вход. Зафиксировано в Non-Goals.

## Migration Plan

- Развёртывание за один коммит: модели → менеджер → форматтер/нотификатор → порты → оркестратор → тесты.
- Откат: revert коммита; поведение возвращается к прежнему (сигналы показываются, недопуски молчат). Данные не мигрируются — новый тип только в коде.