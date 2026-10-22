## Context

`_launch` (`run.py`) сегодня делает всё по порядку:

1. блокировка каталога и журнал;
2. в эмуляции — ранний сбор данных (`_start_early_market_data`: рыночная база, арбитр, календарь, кэш свечей, владелец входа, полосы), локальный API и `LaunchControl`;
3. выбор инструментов (`_choose_instruments`);
4. в истории — план контрактной цепочки, расписание, предпроверка;
5. каналы и шина событий; в истории — рыночные данные (`_build_market_data`) с виртуальными часами;
6. регистрация фильтра `triple_screen` с поставщиком старших ТФ;
7. кластерный фильтр (`_cluster_gate`): в эмуляции фоновый потребитель Redis и вердикт, в истории копия хранилища и вердикт;
8. торговая часть (`_build_runtime`: TradeManager, симулятор, Storage, CSV, аудит);
9. в эмуляции — приём свечей (`_start_live_ingestion`), ожидание прогрева, обработчик паттернов;
10. оркестратор (`_run_bot` → `TradingBot`);
11. остановка: паттерны, поллер, кластерный поток, API, отчёт истории, каналы.

Связи между будущими ядром и контуром, которые надо развязать:

- **Кэш свечей общий.** `MarketDataCache` пишут полосы и приём, а читают стратегии, фильтры и сопровождение. Им же владеет планировщик тиков `MultiTimeframeScheduler`.
- **Обязательства.** `_start_live_ingestion` читает `runtime.storage` (финансовую базу контура), считает обязательства и границы исполнения и передаёт их владельцу входа. Каждый тик `post_tick` в `_run_bot` делает то же.
- **Паттерны.** Владелец обработчика паттернов — ядро, но свечи в него отправляет `TradingBot` после тика.
- **Ссылки через `runtime`.** `runtime.ingestion_worker`, `runtime.execution_aliases`, `runtime.execution_instruments` записываются приёмом данных в объект торговой части.

Шестнадцать тестовых файлов подменяют функции `run.py` (`_build_runtime`, `trading_enabled`, `_risk_limits`, `_load_contracts_metadata`, `_start_early_market_data`, `_run_bot` и другие).

## Goals / Non-Goals

**Goals:**
- Два объекта сборки внутри одного процесса: ядро и торговый контур.
- Контур не знает, откуда данные: живые от ядра или исторические от эмулятора.
- Ядро не читает финансовую базу контура и не импортирует модули контура.
- Поведение, вывод и результаты не меняются.

**Non-Goals:**
- Отдельный процесс контура и чтение ленты `changes` (П3).
- `core.sqlite3`, поколения, новые команды API (П4).
- Состав контура на ходу (П5).
- Перенос триггера паттернов из торгового цикла в ядро: меняет момент отправки свечи, делается в П3.
- Разделение конфигурации на файлы контуров и канал Telegram из конфига контура (П3).

## Decisions

### Пакет `src/assembly/`

```text
src/assembly/
├── services.py   # MarketServices, ContourDemand
├── core.py       # LiveCore — ядро эмуляции
├── history.py    # HistoricalMarketServices — данные прогона истории
└── contour.py    # ContourSettings, TradingContour
```

Имя пакета говорит о роли: сборка объектов, а не новая предметная логика. Предметные пакеты (`data`, `trade_management`, `bot` …) не меняются. Направление зависимостей: `contour.py` → `services.py` ← `core.py`, `history.py`. Ядро и историческая служба не импортируют `contour.py`; это проверяет тест на импорты.

### Порт рыночных служб

```python
class MarketServices(Protocol):
    timeline: MultiTimeframeScheduler
    data_cache: MarketDataCache
    market_store: MarketDataStore | None
    market_calendar: MarketCalendar | None
    request_budget: RequestBudget | None
    pattern_sink: PatternWorker | None          # только эмуляция

    def cluster_gate(self, state_dir) -> ClusterGate | None: ...
    def serve(self, demand: ContourDemand) -> Callable[[], bool] | None: ...
    def update(self, demand: ContourDemand) -> None: ...
    def close(self) -> None: ...
```

- `serve` — регистрация потребностей контура и запуск приёма. В эмуляции: снятие невыбранных пар сохранённого состава, запрос истории для обязательств, нижние границы исполнения, поток, старт поллера. Возвращает ожидание прогрева (`_live_startup_waiter`). В истории ничего не запускает и возвращает `None`.
- `update` — то, что сейчас делает `post_tick`: новые обязательства после тика.
- `cluster_gate` — вердикт кластерного фильтра для контура. В эмуляции служба отдаёт вердикт поверх своего хранилища и потребителя Redis, в истории — поверх копии хранилища в каталоге прогона, как сейчас.
- `timeline` и `data_cache` остаются общими объектами внутри процесса. В П3 контур получит свою реализацию `MarketServices`, которая строит собственные кэш и планировщик из ленты ядра; контур при этом не изменится.

### Потребности контура

```python
@dataclass(frozen=True)
class ContourDemand:
    instruments: tuple[Instrument, ...]                 # инструменты исполнения
    aliases: Mapping[str, str]                          # тикер/UID → ключ инструмента
    obligations: tuple                                  # _market_obligations(...)
    history_requests: tuple[tuple[Instrument, str, int], ...]   # (инструмент, ТФ, баров)
    execution_lower: Mapping[str, datetime]             # нижние границы исполнения
```

Контур собирает потребности сам: читает свою финансовую базу главным потоком (`load_trades`, `execution_bar_boundaries`), учитывает роли жизненного цикла контрактов. Это ровно тот код, что сейчас стоит в `_start_live_ingestion` и `post_tick`, перенесённый на сторону контура. Требование «снимок незавершённых сделок читается главным потоком» сохраняется. Ссылки `runtime.ingestion_worker`, `execution_aliases`, `execution_instruments` больше не нужны: контур держит алиасы у себя, а ядро получает их в `ContourDemand`.

### Настройки контура

```python
@dataclass(frozen=True)
class ContourSettings:
    kind: str                      # emulation | history
    state_dir: Path
    channel_names: list[str] | None
    clock: HistoricalClock | None
    session: RunSession
    client_provider: object
    instruments: list[Instrument] | HistorySelection
    market_data_sync: MarketDataSync | None = None
```

Здесь только то, что различается между контурами. Общие настройки робота (стратегии, профили, риск, фильтры) по-прежнему читаются из `src.config`: конфигурация одна на весь робот.

### Торговый контур

`TradingContour(settings, services)`:

- `build()` — каналы и шина, регистрация `triple_screen` с поставщиком старших ТФ по `services.data_cache`, кластерный вердикт из `services.cluster_gate`, торговая часть (`_build_runtime` переезжает сюда как есть).
- `demand()` — `ContourDemand` по описанным выше правилам.
- `run(waiter)` — `TradingBot` (бывший `_run_bot`), `post_tick` вызывает `services.update(self.demand())`, обработчик паттернов — `services.pattern_sink`.
- `finish()` — отчёт истории (бывший `_finish_history`).
- `close()` — каналы.

`PROFILES["triple_screen"]` — глобальный реестр фильтров. Контур перезаписывает его при сборке, как сейчас. Пока в процессе один контур, это безопасно; в П3 контур и так получает свой процесс.

### Ядро эмуляции

`LiveCore(state_dir, client_provider, control)`:

- `start()` — бывший `_start_early_market_data`: рыночная база, арбитр, календарь, сверка, кэш, владелец входа, полосы по сохранённому составу.
- `start_api()` — бывший вызов `_start_local_api` с `LaunchControl`.
- `cluster_gate()`, `serve()`, `update()` — по порту.
- `pattern_sink` — `PatternWorker`, если `patterns_enabled`; стартует в `serve`, как сейчас после сборки торговой части.
- `close()` — паттерны, поллер или ранние полосы, кластерный поток, API — в прежнем порядке.

Без `independent_lanes` ядро строит данные тем же `_build_market_data`, но после выбора инструментов, как сейчас.

### Историческая служба

`HistoricalMarketServices(state_dir, clock, client_provider, market_calendar)` — бывшая ветка `history` в `_launch`: `_build_market_data(history=True)`, границы предпроверки (`set_boundary_checks`), кластерный вердикт по копии хранилища. `serve` и `update` ничего не делают, `pattern_sink` — `None`.

### Новый `_launch`

```text
lock + журнал
если эмуляция: core = LiveCore(...).start(); core.start_api()
instruments = _choose_instruments(...)            # без изменений
если история: план цепочки, расписание, предпроверка (без изменений); services = HistoricalMarketServices(...)
иначе: services = core
contour = TradingContour(ContourSettings(...), services).build()
waiter = services.serve(contour.demand())
try: contour.run(waiter)
finally: services.close(); если история: contour.finish(); contour.close()
```

Порядок остановки совпадает с сегодняшним: поллер и полосы, кластерный поток, API, отчёт истории, каналы.

### Тесты

- Подмены `run.<функция>` для перенесённых функций переводятся на `src.assembly.<модуль>.<функция>`. Проверки внутри тестов не меняются. Подмены того, что остаётся в `run.py` (`select_instruments`, `_resolve_request`, `_run_live`, `_launch` …), не трогаются.
- Новые тесты: направление импортов; `ContourDemand` из финансовой базы совпадает с тем, что раньше вычислял `_start_live_ingestion`; историческая служба не запускает приём; порядок остановки.
- Страховка: исторический прогон из интеграционных тестов до и после разреза даёт те же сделки и тот же отчёт. Существующий `test_repeated_run_is_identical` и интеграционные тесты истории проходят без правок.

## Risks / Trade-offs

- [Тонкое изменение порядка инициализации ломает ранний сбор или ожидание прогрева] → порядок вызовов в новом `_launch` повторяет старый шаг в шаг, интеграционные и юнит-тесты раннего старта (`test_early_start`, `test_live_roster_modes`) проходят без изменения проверок.
- [Тесты подменяют функции по старому адресу и молча перестают что-то проверять] → после переноса по каждой подмене проверяется, что тест падает, если подменённую функцию сломать (выборочно), а `grep` по `run.<перенесённое имя>` в тестах пуст.
- [Общие `timeline` и `data_cache` маскируют зависимость контура от ядра] → контур обращается к ним только через `MarketServices`; тест на импорты не даёт контуру импортировать `core.py`.
- [Паттерны остаются в торговом цикле] → осознанно, переносится в П3 вместе с отдельным процессом.
