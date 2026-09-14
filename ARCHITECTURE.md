# Архитектура My_roBot

Торговый робот, работающий с Tinkoff Invest API. Запускается из `run.py`, собирается в
один автономный бинарник через PyInstaller (`run.spec`, см. `README.txt`).

Ритм работы — **«один тик = закрытая свеча любого активного таймфрейма»**: итерация
выравнивается по ближайшей границе закрытия свечи среди ТФ привязок, решения
принимаются только по готовым (закрытым) барам; на тике обрабатываются привязки
тех ТФ, чья свеча закрылась. Уведомление выполняется на тике по каждой обработанной
привязке «инструмент × стратегия × профиль × ТФ».

## Принципы модульности

- **Один пакет — одна сквозная ответственность.** Внутренности модулей не смешиваются;
  каждая папка в `src/` закрывает свою область.
- **Композиция через конструктор.** Зависимости собираются в `run.py` без DI-фреймворка.
  `TradingBot` не знает конкретных реализаций — получает интерфейсы (`notifier`,
  `execution`, `data_cache`, `context_cache`, `signal_filter`, `risk_manager`).
- **Стратегии зарегистрированы в реестре.** Имя стратегии → класс и конфиг; назначение
  «стратегия × инструмент» задаётся в конфиге `robot.toml` (таблицы тикеров
  `strategies.share.<ТИКЕР>`/`strategies.future.<БАЗА>` с гибридным массивом
  `strategies`: строка — дефолты тикера, инлайн-таблица `{name, filter, tf}` —
  точечные профиль фильтрации и таймфрейм; каскад ТФ: `tf` → `timeframe` тикера →
  `[robot].timeframe`) и подхватывается через
  `SHARE_STRATEGIES`/`FUTURE_STRATEGIES` из `src/config.py` (списки `Assignment`).
- **Исполнение отделено от принятия решений.** Порт исполнения выбирается в `run.py` по
  признаку «торговый режим активен» (секция `[trading]` в конфиге): с конфигом — имитация
  через `BrokerExecutionPort` (журнал сделок `trade_journal.csv`), без конфига — безопасный
  `NotifyOnlyExecutionPort`. Замена на реальный API не затрагивает оркестратор.

## Модули и их ответственность

| Модуль | Файлы | Ответственность |
|---|---|---|
| `instruments` | `model.py`, `selector.py` | Тип `Instrument`, нормализация, выбор инструментов при старте. |
| `api` | `client.py`, `instruments.py`, `retry.py` | Обёртка над Tinkoff API (потоки свечей, ретраи). |
| `data` | `cache.py`, `loader.py` | Кэш рыночных данных по парам (инструмент, ТФ) и загрузка свечей (`load_candles`). |
| `market_context` | `context_cache.py`, `models.py`, `sr_levels.py`, `trend.py` | Рыночный контекст по парам (инструмент, ТФ): тренд, уровни поддержки/сопротивления. |
| `market_structure` | `swings.py`, `harmonic.py`, `fibonacci.py` | Структура рынка: свинг-детектор, формация AB=CD, уровни Фибо. |
| `strategies` | `*_strategy.py`, `base_strategy.py`, `registry.py`, `signals.py`, `contracts.py`, `indicators/` | Сигнальные стратегии, их построение и реестр. |
| `decision` | `filter.py`, `filters/`, `risk.py` | Пост-фильтр сигналов по профилям (`raw`, `basic_levels`; фабричный выбор по имени в фасаде `SignalFilter`) и риск-менеджмент. |
| `execution` | `port.py` | Исполнение решений: `NotifyOnlyExecutionPort` (без конфига `[trading]`), `BrokerExecutionPort` — обёртка брокерского адаптера, `NotifyOnlyExecutionPort` fallback. |
| `trade_journal` | `journal.py` | **Дневник сделок**: append-only CSV (`trade_journal.csv`, UTF-8-SIG), события `NEW/FILLED/CANCELLED/EXPIRED/CLEARING`, `position_id = {тикер}-{uuid}`, звуковые/когерентные снимки; `replay_events()` восстанавливает баланс, позиции и заявки при рестарте. |
| `portfolio` | `models.py` | **Портфель и риск**: `Account` (баланс = депозит + реализованный P/L + плавающий P/L mark-to-market, equity), `PositionManager` (открытие/закрытие, частичный выход, перекос `over_risk`, лимит агрегированного риска и ГО-ёмкость). |
| `broker` | `port.py`, `journal_broker.py`, `exec_adapter.py` | **Имитация исполнения**: `JournalBroker` (порт границ `BrokerPort`) — жизненный цикл заявок, стоп/тейк-протекшн, TTL, клиринг FORTS, FIFO-отмена при перекосе; `BrokerExecutionAdapter` — мост Сигнал→Сигнал→сайзинг→заявка. |
| `notifier` | `base.py`, `console.py`, `telegram.py` | Доставка уведомлений (консоль / Telegram), включая события имитации (`DealEventFormatter`, `AbstractNotifier.notify_event`). |
| `scheduler` | `timing.py` | `MultiTimeframeScheduler` — координатор сеток активных ТФ (тик = ближайшая граница любого ТФ); `CandleScheduler` — математика одной сетки. |
| `config.py` | — | Параметры из `robot.toml` (внешнего или вшитого `default.toml`) и токены из `.env`; импортируется всеми модулями. |
| `config_loader.py` | — | Загрузка и валидация TOML-конфигурации; приоритет: внешний `robot.toml` → вшитый `default.toml` → дефолты кода; незнакомые ключи/типы → `ConfigError`. |
| `logging_setup.py` | — | Технический журнал: `bot_debug.log` (RotatingFileHandler) с `service_uid`/`correlation_id` из contextvars; консоль не используется, UI — через `notifier`. |
| `bot` | `trading_bot.py` | **Оркестратор**: связывает модули в сценарий (главный цикл). |

## Поток данных

```
run.py  (композиция зависимостей)
   │
   ├─ instruments.selector ───────────────► список инструментов
   ├─ scheduler.timing ───────────────────► ритм (CandleScheduler)
   ├─ data.cache ─┐                        (кэш)
   ├─ api.client ─┴─► data.loader ────────► свечи
   │
   └─► TradingBot.run()  (главный цикл, один тик = одна закрытая свеча)
          │
          ├─ data_cache            свечи по инструменту
          ├─ market_context        тренд + SR-уровни (контекст сделки)
          ├─ strategies.registry   стратегия по паре «инструмент × стратегия»
          │        └─ market_structure  (свинги / Фибо / формация)
├─ decision.signal_filter пост-фильтр сигнала
           ├─ decision.risk_manager  оценка риска
           ├─ execution.port         исполнение (NotifyOnly | BrokerExecutionPort)
           │        └─ broker.exec_adapter → journal_broker → portfolio → trade_journal
           └─ notifier               уведомление пользователя (вкл. события сделок)
```

Шаги в `TradingBot` — отдельные методы-этапы сценария: новый тик → запросить
закэшированные свечи → построить контекст → прогнать стратегию → отфильтровать →
оценить риск → исполнить → уведомить → heartbeat.

## Торговая имитация (`[trading]`)

Включается наличием секции `[trading]` в конфиге (`initial_deposit`, `max_risk_pct`,
`journal_file`, `clearing_times`). Поток в `run.py`:

1. `_build_execution()`: при активном режиме собирает `TradeJournal` + `PortfolioManager` +
   `JournalBroker` и возвращает `(BrokerExecutionPort, on_bar)`; иначе —
   `(NotifyOnlyExecutionPort, None)`. Метаданные контрактов фьючерсов подгружаются из
   реального Tinkoff API (`load_futures_contracts`); при отсутствии — входы по таким
   инструментам отклоняются (`no-contract-meta`).
2. Решение → `BrokerExecutionPort`: сначала дублируется в notifier прежней строкой
   сигнала (`notify_decision`), затем сайзинг (риск `max_risk_pct × equity`, количество
   `floor(risk_rub / (|вход−стоп| × step_cost / price_step))`, мин. 1) → `place_order`.
3. `on_bar` на каждом тике вызывает `run_clearing_if_due()` и `track_bar()`:
   - клиринг (заявки закрываются, защитные переставляются, снимок в журнал);
   - перекос „over_risk" (стоимость позиции > 3 × `INITIAL_DEPOSIT`): FIFO-отмена
     неисполненных заявок других позиций и контр-сделка;
   - защитные стоп/тейк + TTL (до 30m → 1h; до 1h → 4h; ≥4h → ближайший клиринг).
4. Все события журналируются append-only в `trade_journal.csv` и дублируются в notifier
   (`notify_event`). Рестарт восстанавливает состояние через `TradeJournal.replay(initial_deposit)`
   (в т.ч. отменённые/истёкшие заявки отрезают «призрачные» NEW-строки).

## Стратегии

- Каждая стратегия — класс, построенный по контрактам `strategies/contracts.py`
  (`SignalType`, `Decision` и т.п.) и базовому классу `base_strategy.py`.
- Индикаторы живут отдельно в `strategies/indicators/` и переиспользуются стратегиями.
- `registry.get_strategy(name)` мапит имя → класс/конфиг; `validate_assignments`
  сверяет назначения «какая стратегия на какой инструмент».
- Реализованные стратегии: `macd_rsi_stoch`, `flat_triangle`, `harmonic_abcd`, `ma_cloud_rsi_macd`.

## Вспомогательное

- `tests/` — автотесты; `tests/snapshot/` — снапшот-тесты стратегий и визуализации
  (кейсы с эталонными сигналами в `tests/snapshot/data/`).
- `tools/visualize_signals.py` — рендер SVG-картинок сигналов для спецификаций.
- `openspec/` — спецификации и исторические изменения (OpenSpec): каждая фича
  прорабатывается через proposal → design → specs → tasks, затем архивируется.
- Выпуск: версия живёт в `src/__init__.py` (`__version__`, SemVer), изменения — в
  `CHANGELOG.md`; сборка `run.spec` выдаёт `dist/robot-vX.Y.Z` рядом с `robot.toml`
  и `robot-vX.Y.Z.txt` (тезисы версии из CHANGELOG), токены — только в `.env`.
