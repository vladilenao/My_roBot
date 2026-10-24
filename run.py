
import src.grpc_fork  # noqa: F401  — первым, до загрузки gRPC
from src import __version__
from datetime import datetime, timedelta
from pathlib import Path
from shutil import rmtree
import sys

from src.config import (
    ACTIVE_TIMEFRAMES,
    CONTOURS_FEED_POLL_SECS,
    MARKET_DATA_DATABASE_FILE,
    trading_enabled,
    DATA_REFRESH_MIN_INTERVAL,
    MARKET_DATA_EXPORT_ENABLED,
    MARKET_DATA_SETTINGS,
    MARKET_DATA_EXPORT_TOKEN,
    ROBOT_CONTROL_TOKEN,
    MARKET_STRUCTURE_REDIS_PASSWORD,
    FUTURE_STRATEGIES,
    SHARE_STRATEGIES,
    TICKER,
    TINKOFF_TOKEN,
    INSTRUMENT_TYPE,
    LOGGING_SERVICE_UID,
    LOGGING_FILE,
    LOGGING_LEVEL,
    LOGGING_MAX_BYTES,
    LOGGING_BACKUP_COUNT,
    TELEGRAM_BOT_TOKEN,
    CLOUDFLARE_URL,
    CONTRACT_EXPIRY_BLOCK_DAYS,
    CONTRACT_EXPIRY_CLOSE_BEFORE_END_MIN,
    INTRADAY_SETTINGS,
    runtime_dir,
    run_dir_name,
    HIST_DIR,
    HISTORICAL_LOGGING,
    HISTORICAL_SCHEDULE,
    HISTORICAL_ROLLOVER,
)
from src.api.client import client_context, default_client_provider
from src.api.emulator_client import EmulatorClientProvider, EmulatorError
from src.bot.launch import (
    HISTORY_DEFAULT_PAUSE,
    MODE_EMULATION,
    SOURCE_API,
    SOURCE_MENU,
    LaunchError,
    LaunchRequest,
    Rejection,
    RunConfigurationError,
    describe_choice,
    equivalent_command,
    parse_launch_args,
    parse_moment,
    resolve_emulation,
    resolve_history,
    tokens_from_menu,
    validate_range,
    without_assignments,
)
from src.assembly.contour import ContourSettings, TradingContour, _family_warmup, _strategy_map
from src.assembly.core import LiveCore, _say
from src.assembly.history import HistoricalMarketServices
from src.assembly.core_state import CORE_DATABASE_FILE, CoreStateStore
from src.assembly.process import JournalMigrationError, contour_dir
from src.assembly.services import _timeframes
from src.bot.launch_control import LaunchControl
from src.bot.run_session import build_run_session
from src.data.timeutil import to_naive
from src.history.preflight import active_pairs, run_preflight, run_chain_preflight, smallest_period
from src.history.selection import HistorySelection
from src.history.report import (
    MarketDataSync,
)
from src.scheduler.clock import HistoricalClock
from src.data.loader import load_candles
from src.instruments import normalize_instrument
from src.instruments.selector import select_instruments
from src.logging_setup import get_logger, setup_logging
from src.runtime_lock import RuntimeLockError, acquire_runtime_lock

log = get_logger(__name__)

MODE_LIVE = "live"
MODE_HISTORY = "history"
MOMENT_FORMAT = "%Y-%m-%d %H:%M"


def ask_mode(no_prompt: bool = False) -> str:
    """Режим прогона. ``--no-prompt`` — эмуляция без единого вопроса."""
    if no_prompt:
        return MODE_LIVE
    print("=== Режим работы ===")
    print("  1. Эмуляция (живые данные, симулятор исполнения)")
    print("  2. Историческая торговля (локальный эмулятор данных, заданный диапазон)")
    while True:
        answer = input("Выбор режима (1/2): ").strip()
        if answer == "1":
            return MODE_LIVE
        if answer == "2":
            return MODE_HISTORY
        print("Нужен ответ 1 или 2.")


def ask_emulation_source() -> bool:
    """Меню эмуляции: ``True`` — ждать команду API, ``False`` — выбрать инструменты сейчас."""
    print("Инструменты: выбрать сейчас — 1, ждать команду API — 2")
    while True:
        answer = input("Ваш выбор (1/2): ").strip()
        if answer in ("1", ""):
            return False
        if answer == "2":
            return True
        print("Нужен ответ 1 или 2.")


def ask_history_range() -> tuple[datetime, datetime, float]:
    """Границы исторического прогона и пауза между тиками в секундах."""
    start = _ask_moment("Начало диапазона")
    end = _ask_moment("Конец диапазона (не входит в прогон)")
    pause = _ask_pause()
    return validate_range(start, end, pause)


def _ask_moment(prompt: str) -> datetime:
    while True:
        raw = input(f"{prompt} (ГГГГ-ММ-ДД ЧЧ:ММ): ").strip()
        try:
            return parse_moment(raw)
        except ValueError as exc:
            print(str(exc))


def _ask_pause() -> float:
    while True:
        raw = input(f"Пауза между тиками, с (Enter — {HISTORY_DEFAULT_PAUSE:g}): ").strip()
        if not raw:
            return HISTORY_DEFAULT_PAUSE
        try:
            value = float(raw)
        except ValueError:
            print("Пауза должна быть числом секунд.")
            continue
        if value < 0:
            print("Пауза не может быть отрицательной.")
            continue
        return value


def describe_scale(start: datetime, end: datetime, pause: float, timeframes=None) -> str:
    """Оценка объёма прогона: тики, реальное время и каталог состояния."""
    step = smallest_period(timeframes or (sorted(set(ACTIVE_TIMEFRAMES) | {"1m"})))
    ticks = int((to_naive(end) - to_naive(start)).total_seconds() // step.total_seconds())
    if pause:
        waiting = f"Оценка реального времени при паузе {pause:g} с: {_duration(timedelta(seconds=ticks * pause))}"
    else:
        waiting = "Ожидание между тиками: нет (прогон без пауз, длительность определяется обработкой)"
    return (
        f"=== Масштаб прогона ===\n"
        f"Шаг тика:        {step}\n"
        f"Обработано тиков: {ticks}\n"
        f"{waiting}\n"
        f"Каталог состояния: {state_dir_for(start, end)}"
    )


def _duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


def state_dir_for(start: datetime, end: datetime, started_at: datetime | None = None) -> Path:
    """Каталог состояния исторического прогона — отдельный от боевого."""
    return runtime_dir(Path(HIST_DIR)) / run_dir_name(
        to_naive(start), to_naive(end), to_naive(started_at or datetime.now())
    )


def main(argv=None, no_prompt: bool = False):
    """Точка входа: параметры командной строки или меню, затем общий путь запуска."""
    argv = list(argv or [])
    if argv[:1] == ["--contour"]:
        # Служебный вход процесса контура: его запускает ядро (add-contour-process).
        return _run_contour(argv[1] if len(argv) > 1 else "")
    if no_prompt and "--no-prompt" not in argv:
        argv.append("--no-prompt")
    try:
        request = parse_launch_args(argv)
    except LaunchError as exc:
        print(str(exc))
        return 2
    if request is None:
        try:
            request = _menu_request()
        except (RunConfigurationError, ValueError) as exc:
            print(str(exc))
            return 1
    if request.mode == MODE_HISTORY:
        print(describe_scale(request.start, request.end, request.pause))
        return _run_history(request.start, request.end, request.pause, request)
    return _run_live(request)


def _run_contour(kind: str) -> int:
    """Процесс торгового контура: блокировка и журнал в своей папке, файл запуска, лента ядра (D1)."""
    import signal

    from src.assembly.contracts import contracts_from_payload, instrument_from_payload
    from src.assembly.contour_control import STATUS_FILE, ContourControl
    from src.config import DATABASE_FILE
    from src.assembly.feed import FeedMarketServices
    from src.assembly.process import LaunchFileError, StdinStop, read_launch
    from src.bot.run_session import StoppableRunSession
    from src.data.market_store import MarketDataStore

    if kind != MODE_EMULATION:
        print(f"Неизвестный вид контура {kind!r}: поддерживается только {MODE_EMULATION}.")
        return 2
    data_dir = runtime_dir()
    directory = contour_dir(data_dir, kind)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        acquire_runtime_lock(directory)
    except RuntimeLockError as exc:
        print(str(exc))
        return 1
    setup_logging(
        service_uid=LOGGING_SERVICE_UID, log_file=str(directory / LOGGING_FILE),
        secrets=(TINKOFF_TOKEN, TELEGRAM_BOT_TOKEN, CLOUDFLARE_URL,
                 MARKET_DATA_EXPORT_TOKEN, ROBOT_CONTROL_TOKEN, MARKET_STRUCTURE_REDIS_PASSWORD),
        **_log_settings(False, None),
    )
    log.info("Контур %s v%s запущен", kind, __version__)
    try:
        launch = read_launch(directory, kind)
    except LaunchFileError as exc:
        log.error("%s", exc)
        print(str(exc))
        return 2
    stop = StdinStop()
    stop.start()
    # Ctrl+C в консоли доходит и до контура: останавливаемся на границе тика, а не посреди него.
    signal.signal(signal.SIGINT, lambda *_: stop.request("Ctrl+C"))
    # Желаемое ядра читается на границе тика; завершение остановки ставит тот же флаг, что stdin.
    control = ContourControl(core_db=data_dir / CORE_DATABASE_FILE, contour_id=kind,
                             journal_db=directory / DATABASE_FILE, status_path=directory / STATUS_FILE,
                             request_stop=stop.request, version=__version__)
    instruments = [instrument_from_payload(item) for item in launch["instruments"]]
    services = FeedMarketServices(MarketDataStore(data_dir / MARKET_DATA_DATABASE_FILE), contour_id=kind,
                                  data_dir=data_dir, tracks_obligations=trading_enabled(),
                                  poll_seconds=CONTOURS_FEED_POLL_SECS)
    contour = TradingContour(ContourSettings(
        kind=kind, state_dir=directory, channel_names=None, clock=None,
        session=StoppableRunSession(stop.requested), client_provider=None, instruments=instruments,
        contracts=contracts_from_payload(launch.get("contracts") or {}), control=control,
    ), services)
    try:
        contour.build()
        contour.run(services.serve(contour.demand()))
    finally:
        services.close()
        contour.close()
    log.info("Контур %s остановлен: %s", kind, stop.reason or "цикл завершён")
    return 0


def _menu_request() -> LaunchRequest:
    """Меню собирает те же параметры запуска, что командная строка."""
    if ask_mode() == MODE_HISTORY:
        start, end, pause = ask_history_range()
        return LaunchRequest(MODE_HISTORY, SOURCE_MENU, None, start=start, end=end, pause=pause)
    return LaunchRequest(MODE_EMULATION, SOURCE_MENU, None, wait_command=ask_emulation_source())


def _run_live(request: LaunchRequest | None = None):
    try:
        code, _, _ = _launch(
            state_dir=runtime_dir(),
            client_provider=None,
            clock=None,
            session=build_run_session(MODE_LIVE),
            channel_names=None,
            launch=request,
        )
    except RuntimeLockError as exc:
        print(str(exc))
        return 1
    return code


def _run_history(start, end, pause, request: LaunchRequest | None = None):
    """Прогон с повтором, если проверка сдвинула начало на первую доступную свечу.

    Сдвиг меняет часы и каталог состояния, поэтому прогон собирается заново; уже
    выбранные инструменты передаются в повтор, чтобы пользователя не спрашивали
    второй раз. Повтор ровно один — границы для нового начала проверены.
    """
    provider = EmulatorClientProvider()
    try:
        sync = provider.prepare_snapshot()
    except EmulatorError as exc:
        market_data_sync = MarketDataSync(reason=exc.message)
        print(f"Импорт My Robot перед прогоном не подтверждён: {exc.message}")
    else:
        if sync["synchronized"]:
            market_data_sync = MarketDataSync(
                status="synchronized",
                producer_id=sync["producer_id"],
                target_change_id=sync["target_change_id"],
                after_id=sync["after_id"],
                snapshot_generation=sync["snapshot_generation"],
            )
            print(
                "Импорт My Robot подтверждён: "
                f"снимок {sync['snapshot_generation']}, "
                f"курсор {sync['after_id']}/{sync['target_change_id']}."
            )
        else:
            source = sync.get("source")
            state = source.get("state") if isinstance(source, dict) else None
            reason = (
                f"эмулятор сообщил состояние {state}"
                if state in {"incomplete", "blocked", "error"}
                else "эмулятор не подтвердил полноту журнала"
            )
            market_data_sync = MarketDataSync(
                producer_id=sync["producer_id"],
                target_change_id=sync["target_change_id"],
                after_id=sync["after_id"],
                reason=reason,
            )
            print(f"Импорт My Robot перед прогоном не подтверждён: {reason}")
    instruments = None
    for _ in range(2):
        clock = HistoricalClock(start, end, smallest_period(_timeframes()), pause)
        state_dir = state_dir_for(start, end)
        code, shifted, instruments = _launch(
            state_dir=state_dir,
            client_provider=provider,
            clock=clock,
            session=build_run_session(MODE_HISTORY, clock),
            channel_names=[],
            history=True,
            instruments=instruments,
            market_data_sync=market_data_sync,
            launch=request,
        )
        if shifted is None:
            return code
        rmtree(state_dir, ignore_errors=True)
        start = shifted
    return code


def _warmup_bars() -> int:
    """Сколько баров прогрева требуют активные стратегии — проверка границ."""
    from src.strategies.registry import get_strategy

    needed = 1
    for assignments in (SHARE_STRATEGIES, FUTURE_STRATEGIES):
        for group in assignments.values():
            for assignment in group:
                config = _strategy_map().get(assignment.strategy)
                if config is None:
                    continue
                try:
                    needed = max(
                        needed, get_strategy(assignment.strategy, config).required_history()
                    )
                except Exception as exc:  # noqa: BLE001 — стратегию проверит бот при старте
                    log.debug("Прогрев для %s не посчитан: %s", assignment.strategy, exc)
    return needed


def _source_ping(client_provider):
    """Проверка живости источника: у эмулятора есть ``/health``, у T-API нет."""
    client_context = getattr(client_provider, "client_context", None)
    if client_context is None:
        return None

    def ping() -> None:
        with client_context() as client:
            probe = getattr(client, "ping", None)
            if probe is not None:
                probe()

    return ping


def _source_coverage(client_provider):
    """Счётная проверка покрытия на источнике: у эмулятора есть ``/coverage``."""
    client_context = getattr(client_provider, "client_context", None)
    if client_context is None:
        return None

    def coverage(*, instrument_id, timeframe, start_date, end_date):
        with client_context() as client:
            probe = getattr(client, "coverage", None)
            if probe is None:
                raise EmulatorError("UNSUPPORTED", "источник не умеет проверять покрытие")
            return probe(instrument_id=instrument_id, timeframe=timeframe,
                         from_=start_date, to=end_date)

    return coverage


def _historical_calendar_expectation(market_calendar):
    """Загружает календарь снимка только во время предпроверки границы."""
    from src.market_calendar.rules import candle_rule

    def expectation(pair, now):
        if not pair.instrument_id:
            return None
        timeout = MARKET_DATA_SETTINGS.candle_publication_timeout_by_tf.get(
            pair.timeframe, MARKET_DATA_SETTINGS.candle_publication_timeout_secs,
        )
        result = market_calendar.expectation(
            pair.instrument_id, candle_rule(pair.timeframe), to_naive(now).to_pydatetime(),
            timedelta(seconds=timeout),
        )
        if result.phase.value == "unknown":
            # В отличие от live-тиков preflight допускает один запрос к
            # эмулятору, чтобы проверить границу того же snapshot.
            market_calendar.service(to_naive(now).to_pydatetime())
            result = market_calendar.expectation(
                pair.instrument_id, candle_rule(pair.timeframe), to_naive(now).to_pydatetime(),
                timedelta(seconds=timeout),
            )
        return result

    return expectation


def _log_settings(history: bool, clock) -> dict:
    """Порог и ротация лога: у исторического прогона своя секция [historical.logging]."""
    if not history:
        return {"level": LOGGING_LEVEL, "max_bytes": LOGGING_MAX_BYTES, "backup_count": LOGGING_BACKUP_COUNT}
    window = HISTORICAL_LOGGING["debug_from"], HISTORICAL_LOGGING["debug_to"]
    return {
        "level": HISTORICAL_LOGGING["level"],
        "max_bytes": HISTORICAL_LOGGING["max_bytes"],
        "backup_count": HISTORICAL_LOGGING["backup_count"],
        "clock": None if clock is None else clock.now,  # HistoricalClock: naive UTC
        "debug_window": None if window[0] is None else window,
    }


def _launch(
    *,
    state_dir,
    client_provider,
    clock,
    session,
    channel_names,
    history: bool = False,
    instruments=None,
    market_data_sync: MarketDataSync | None = None,
    launch: LaunchRequest | None = None,
) -> tuple[int, datetime | None, list]:
    """Единая сборка прогона: рыночные службы, затем торговый контур поверх них.

    В эмуляции службы — ядро (``LiveCore``), в истории — историческая служба
    данных (``HistoricalMarketServices``); контур (``TradingContour``) один и
    тот же, поэтому исторический прогон идёт по тому же конвейеру, что и боевой.

    Возвращает код завершения, сдвиг начала и выбранные инструменты: если
    проверка перенесла начало на первую доступную свечу, собранные часы и каталог
    уже не годятся, а прогонать диапазон должен вызывающий — он же передаст
    сюда ``instruments``, чтобы не спрашивать пользователя повторно.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    if not history:
        acquire_runtime_lock(state_dir)
    setup_logging(
        service_uid=LOGGING_SERVICE_UID,
        log_file=str(state_dir / LOGGING_FILE),
        secrets=(TINKOFF_TOKEN, TELEGRAM_BOT_TOKEN, CLOUDFLARE_URL,
                 MARKET_DATA_EXPORT_TOKEN, ROBOT_CONTROL_TOKEN, MARKET_STRUCTURE_REDIS_PASSWORD),
        **_log_settings(history, clock),
    )
    log.info("Робот v%s запущен", __version__)
    core = None
    if not history:
        core = LiveCore(state_dir, client_provider)
        # Независимые полосы: данные собираются с момента запуска процесса, пока
        # пользователь выбирает инструменты (add-independent-market-data-lanes).
        if MARKET_DATA_SETTINGS.independent_lanes:
            core.start_early()
    # Локальный API — сразу после раннего сбора данных, до выбора инструментов:
    # команда запуска приходит именно сюда (add-noninteractive-launch).
    # Желаемое и журнал команд ядра (add-control-api): записываются при любом запуске эмуляции,
    # иначе контур прочитал бы оставшуюся с прошлого раза остановку.
    core_state = None if history else CoreStateStore(state_dir / CORE_DATABASE_FILE)
    control = (LaunchControl(core_state=core_state, contour_dir=contour_dir(state_dir, MODE_EMULATION),
                             log_file=LOGGING_FILE)
               if not history and ROBOT_CONTROL_TOKEN else None)
    if control is not None:
        if launch is not None and launch.wait_command:
            control.accept_commands()
        else:
            control.mark_starting(SOURCE_MENU if launch is None else launch.source)
    if core is not None and core.early is not None:
        core.start_api(control)
    if instruments is None:
        try:
            instruments = _choose_instruments(
                launch, history=history, client_provider=client_provider, clock=clock,
                early=None if core is None else core.early, control=control,
                export_server=None if core is None else core.export_server,
            )
        except BaseException:
            _stop_core(core)
            raise
        if instruments is None:
            _stop_core(core)
            return 2, None, None
        if not isinstance(instruments, HistorySelection):
            instruments = [normalize_instrument(item) for item in instruments]

    selection = instruments if isinstance(instruments, HistorySelection) else None
    if selection is not None:
        try:
            selection = selection.planned(session.start, session.end, HISTORICAL_ROLLOVER, CONTRACT_EXPIRY_BLOCK_DAYS)
        except ValueError as exc:
            print(f"Прогон невозможен — контрактная цепочка: {exc}")
            return 2, None, selection
        instruments = selection.instruments

    provider = client_provider if history or client_provider is not None else default_client_provider()
    market_calendar = None
    if history:
        from src.market_calendar.service import EmulatorScheduleSource, MarketCalendar, SnapshotScheduleStore

        snapshot_store = SnapshotScheduleStore()
        market_calendar = MarketCalendar(
            snapshot_store,
            EmulatorScheduleSource(snapshot_store, provider),
            schedule_max_age_secs=MARKET_DATA_SETTINGS.schedule_max_age_secs,
            allow_reconstructed=HISTORICAL_SCHEDULE["use_reconstructed"],
        )
        calendar_expectation = _historical_calendar_expectation(market_calendar)
        if selection is not None:
            # Рубежи экспирации уточняются по расписанию цепочки до сохранения и предпроверки.
            _preload_history_schedule(market_calendar, selection, instruments, session)
            try:
                selection = selection.planned(session.start, session.end, HISTORICAL_ROLLOVER, CONTRACT_EXPIRY_BLOCK_DAYS,
                                              cutoff_for=_history_expiry_cutoff(market_calendar))
            except ValueError as exc:
                print(f"Прогон невозможен — контрактная цепочка: {exc}")
                return 2, None, selection
            instruments = selection.instruments
    else:
        calendar_expectation = None
    if selection is not None:
        selection.save(state_dir)
        session.contract_selection = selection

    if history:
        if selection is not None and selection.families:
            report = run_chain_preflight(selection,
                requirements=_family_warmup, start=session.start, end=session.end,
                load_candles=load_candles, client_provider=client_provider, clock=clock,
                ping=_source_ping(client_provider),
                coverage=_source_coverage(client_provider),
                fixed_pairs=active_pairs(selection.fixed, SHARE_STRATEGIES, FUTURE_STRATEGIES),
                fixed_warmup=_warmup_bars(), calendar_expectation=calendar_expectation)
        else:
            report = run_preflight(
                active_pairs(instruments, SHARE_STRATEGIES, FUTURE_STRATEGIES),
                start=session.start,
                end=session.end,
                warmup_bars=_warmup_bars(),
                load_candles=load_candles,
                client_provider=client_provider,
                clock=clock,
                ping=_source_ping(client_provider),
                coverage=_source_coverage(client_provider),
                calendar_expectation=calendar_expectation,
            )
        if not report.ok:
            print(report.message())
            return 2, None, selection or instruments
        if report.start != session.start:
            print(report.message())
            return 3, report.start, selection or instruments
        if report.notes:
            print(report.message())
        session.set_boundary_checks(report.boundaries)
        if selection is None:
            _preload_history_schedule(market_calendar, selection, instruments, session)

    if not history:
        # Контур эмуляции — отдельный процесс на ленте ядра (add-contour-process).
        core.ensure_market_data()
        core.start_api(control)
        def next_launch():
            """После выхода контура — следующая команда API (повторный запуск); без API — Ctrl+C."""
            if control is None or core.export_server is None:
                return None
            chosen = _await_command(control, core.export_server, client_provider,
                                    None if core.early is None else core.early.request_budget, None)
            snapshot = control.snapshot()
            return [normalize_instrument(item) for item in chosen], SOURCE_API, snapshot.get("command") or ""

        try:
            code = core.run_contour(instruments, kind=MODE_EMULATION, control=control, core_state=core_state,
                                    source="" if launch is None else launch.source,
                                    command="" if control is None else control.snapshot().get("command") or "",
                                    next_launch=next_launch)
        except JournalMigrationError as exc:
            print(str(exc))
            code = 2
        finally:
            core.close()
        return code, None, instruments
    services = HistoricalMarketServices(state_dir, clock, provider, market_calendar, report.boundaries)
    contour = TradingContour(ContourSettings(
        kind=MODE_HISTORY, state_dir=state_dir, channel_names=channel_names,
        clock=clock, session=session, client_provider=provider, instruments=instruments,
        market_data_sync=market_data_sync,
    ), services)
    try:
        contour.build()
        contour.run(services.serve(contour.demand()))
    finally:
        services.close()
        contour.finish()
        contour.close()
    return 0, None, selection or instruments


def _stop_core(core) -> None:
    """Запуск отменён до сборки: остановить API и ранние полосы данных."""
    if core is not None:
        core.close()


def _choose_instruments(launch, *, history, client_provider, clock, early, control, export_server):
    """Инструменты прогона из параметров, команды API или меню.

    ``None`` — запуск отменён: параметры отклонены или ждать команду нельзя.
    Без ``launch`` (прямой вызов) работает прежнее меню без фильтра и команды.
    """
    market_now = None if clock is None else clock.now()
    budget = None if early is None else early.request_budget
    if launch is not None and launch.wait_command:
        if control is None or export_server is None:
            if control is None:
                reason = "в .env не задан ROBOT_CONTROL_TOKEN"
            elif not MARKET_DATA_EXPORT_ENABLED:
                reason = "локальный API выключен: в robot.toml [market_data] export_enabled = false"
            elif early is None:
                reason = "локальный API не поднят до выбора инструментов: в robot.toml [market_data] independent_lanes = false"
            else:
                reason = "локальный API не запустился, причина — в строках выше"
            print(f"Ждать команду запуска нельзя: {reason}.")
            return None
        return _await_command(control, export_server, client_provider, budget, market_now)
    if launch is not None and launch.instruments is not None:
        resolution = _resolve_request(launch, client_provider, budget, market_now, history)
        _print_resolution(resolution)
        if not resolution.ok:
            print("Торговля не запущена: исправьте --instruments.")
            return None
        _announce(launch, resolution.chosen, control, launch.source)
        return resolution.instruments
    while True:
        selected = select_instruments(
            validation_pause_secs=0.0 if history else DATA_REFRESH_MIN_INTERVAL,
            client_provider=client_provider,
            market_now=market_now,
            historical=history,
            request_budget=budget,
        ) or [(TICKER, TICKER, INSTRUMENT_TYPE)]
        if launch is None:
            return selected
        chosen = tokens_from_menu(selected, history=history)
        missing = without_assignments(chosen, SHARE_STRATEGIES, FUTURE_STRATEGIES)
        for name in missing:
            print(f"  ✗ {name}: у инструмента нет привязок в robot.toml")
        selected, chosen = _drop_unassigned(selected, chosen, missing, history)
        if chosen:
            break
        print("Необходимо выбрать хотя бы один инструмент с привязками.")
    _announce(launch, chosen, control, SOURCE_MENU)
    return selected


def _drop_unassigned(selected, chosen, missing, history):
    if not missing:
        return selected, chosen
    keep = [item for item in chosen if item.name not in missing]
    if history:
        fixed = tuple(item for item in selected.fixed if (item.short_name or "контракт не указан") not in missing)
        families = tuple(family for family in selected.families if family not in missing)
        return HistorySelection(fixed, families, selected.catalog), keep
    return [item for item in selected if (normalize_instrument(item).short_name or "контракт не указан") not in missing], keep


def _resolve_request(request, client_provider, budget, market_now, history):
    context = client_provider.client_context() if client_provider is not None else client_context()
    with context as raw_client:
        client = raw_client if budget is None else budget.wrap_client(raw_client, lane="interactive")
        if history:
            return resolve_history(request.instruments, client, share_strategies=SHARE_STRATEGIES,
                                   future_strategies=FUTURE_STRATEGIES, market_now=market_now)
        return resolve_emulation(request.instruments, client, share_strategies=SHARE_STRATEGIES,
                                 future_strategies=FUTURE_STRATEGIES, block_days=CONTRACT_EXPIRY_BLOCK_DAYS,
                                 market_now=market_now)


def _print_resolution(resolution) -> None:
    for item in resolution.chosen:
        print(f"  ✓ {describe_choice(item)}")
    for item in resolution.rejections:
        print(f"  ✗ {item.token}: {item.reason}")


def _announce(launch, chosen, control, source, pending=None) -> str:
    command = equivalent_command(launch, chosen)
    print(f"Повторить запуск: {command}")
    log.info("Повторить запуск: %s", command)
    if control is not None:
        control.mark_launched(source, chosen, command, pending)
    return command


def _await_command(control, export_server, client_provider, budget, market_now):
    """Главный поток ждёт команду API на месте выбора инструментов в консоли."""
    control.accept_commands()
    host, port = export_server.address
    _say(f"Робот ждёт команду запуска: POST http://{host}:{port}/api/v1/contours")
    while True:
        pending = control.next_command()
        try:
            resolution = _resolve_request(pending.request, client_provider, budget, market_now, history=False)
        except Exception as exc:  # сбой разрешения не должен оставлять API без ответа
            log.warning("Команда запуска не разрешена: %s", type(exc).__name__)
            control.reject(pending, [Rejection("*", f"Внутренняя ошибка разрешения инструментов ({type(exc).__name__}).")])
            continue
        _print_resolution(resolution)
        if not resolution.ok:
            control.reject(pending, resolution.rejections or [Rejection("*", "Не выбрано ни одного инструмента.")])
            print("Команда запуска отклонена, робот ждёт следующую.")
            continue
        _announce(pending.request, resolution.chosen, control, SOURCE_API, pending)
        return resolution.instruments


def _history_expiry_cutoff(market_calendar):
    """Эффективный рубеж экспирации контракта цепочки по загруженному расписанию прогона."""
    from src.market_calendar.expiry import expiry_cutoff

    margin = timedelta(minutes=CONTRACT_EXPIRY_CLOSE_BEFORE_END_MIN)

    def cutoff_for(card, calendar_cutoff):
        exchange = market_calendar.exchange_for(card.uid)
        # Восстановленные дни календарь отдаёт, только если прогон их разрешил.
        calendar = None if exchange is None else market_calendar.calendar(
            exchange, calendar_cutoff, include_reconstructed=True)
        return expiry_cutoff(calendar, calendar_cutoff, margin)
    return cutoff_for


def _preload_history_schedule(market_calendar, selection, instruments, session) -> None:
    """Расписание прогона до первого тика; пропуски для внутридневных стратегий — предупреждением."""
    from src.history.schedule_notice import preload_history_schedule, report_notice, schedule_ranges

    try:
        ranges = schedule_ranges(selection, instruments, session.start, session.end,
                                 INTRADAY_SETTINGS, SHARE_STRATEGIES, FUTURE_STRATEGIES)
        report_notice(preload_history_schedule(market_calendar, ranges, session.start, INTRADAY_SETTINGS))
    except Exception as exc:  # предзагрузка не должна останавливать прогон
        if getattr(exc, "code", None) == "SNAPSHOT_MISMATCH":
            raise
        log.warning("Предзагрузка расписания прогона не выполнена: %s", type(exc).__name__)


_UTF8_STREAMS_SET = False


def _force_utf8_streams() -> None:
    """Запуск без терминала (pipe, CI) на Windows использует cp1252.

    Замороженный PyInstaller-бинарь не подхватывает PYTHONUTF8 из окружения и
    падает на кириллице при перенаправлении stdout. Локальная консоль не
    трогается: там Python сам выбирает подходящий codepage.
    """
    global _UTF8_STREAMS_SET
    if _UTF8_STREAMS_SET:
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None or getattr(stream, "isatty", lambda: False)():
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    _UTF8_STREAMS_SET = True


def _config_smoke() -> None:
    """Проверяет bundled default.toml без пользовательских файлов и сети."""
    from src.config import _DEFAULTS
    from src.config_loader import load_config

    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    load_config(_DEFAULTS, config_file=root / "missing-robot.toml", bundled_file=root / "default.toml")
    print("Конфигурация по умолчанию загружена.")


def _run_smoke_command(args: list[str]) -> bool:
    """Выполняет неинтерактивные проверки binary и сообщает, была ли команда."""
    if "--version" in args:
        print(__version__)
        return True
    if "--config-smoke" in args:
        _config_smoke()
        return True
    if "--telegram-chart-smoke" in args:
        from src.notifier.telegram_chart import smoke

        smoke()
        return True
    return False


if __name__ == "__main__":
    _force_utf8_streams()
    if _run_smoke_command(sys.argv[1:]):
        pass
    elif any(flag in sys.argv[1:] for flag in ("--telegram-pending", "--telegram-retry", "--telegram-cleanup-files")):
        import argparse
        from src import config
        from src.notifier.telegram_recovery import cleanup, list_pending, retry

        parser = argparse.ArgumentParser(description="Восстановление уведомлений Telegram")
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--telegram-pending", action="store_true")
        action.add_argument("--telegram-retry", action="store_true")
        action.add_argument("--telegram-cleanup-files", action="store_true")
        # Доставка Telegram — журнал контура эмуляции (add-contour-process).
        parser.add_argument("--state-dir", type=Path, default=contour_dir(runtime_dir(), MODE_EMULATION))
        parser.add_argument("--id", type=int, dest="operation_id")
        parser.add_argument("--trade-id")
        parser.add_argument("--from", dest="day_from")
        parser.add_argument("--to", dest="day_to")
        parser.add_argument("--include-uncertain", action="store_true")
        args = parser.parse_args()
        try:
            if args.telegram_pending:
                code = list_pending(config, args.state_dir, trade_id=args.trade_id, day_from=args.day_from, day_to=args.day_to)
            elif args.telegram_cleanup_files:
                code = cleanup(config, args.state_dir)
            else:
                # Ручной replay не должен пересекаться с работающим роботом.
                acquire_runtime_lock(args.state_dir)
                code = retry(config, args.state_dir, operation_id=args.operation_id, trade_id=args.trade_id,
                             day_from=args.day_from, day_to=args.day_to, include_uncertain=args.include_uncertain)
        except (RuntimeLockError, ValueError) as exc:
            print(str(exc))
            code = 2
        sys.exit(code)
    else:
        sys.exit(main(sys.argv[1:]) or 0)
