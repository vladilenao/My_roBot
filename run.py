import src.grpc_fork  # noqa: F401  — первым, до загрузки gRPC
from src import __version__
from datetime import timedelta
from pathlib import Path
import os
import sys

from src.config import (
    CONTOURS_AUTOSTART,
    CONTOURS_FEED_POLL_SECS,
    CONTOURS_HEARTBEAT_SECS,
    CONTOURS_HEARTBEAT_TIMEOUT_SECS,
    CONTOURS_MAX_HISTORY_RUNS,
    MARKET_DATA_DATABASE_FILE,
    trading_enabled,
    MARKET_DATA_SETTINGS,
    MARKET_DATA_EXPORT_HOST,
    MARKET_DATA_EXPORT_PORT,
    MARKET_DATA_EXPORT_TOKEN,
    ROBOT_CONTROL_TOKEN,
    MARKET_STRUCTURE_REDIS_PASSWORD,
    FUTURE_STRATEGIES,
    SHARE_STRATEGIES,
    TINKOFF_TOKEN,
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
    HISTORICAL_LOGGING,
    HISTORICAL_SCHEDULE,
    HISTORICAL_ROLLOVER,
)
from src.api.client import client_context
from src.api.emulator_client import EmulatorClientProvider, EmulatorError
from src.bot.launch import (
    MODE_EMULATION,
    MODE_HISTORY,
    SOURCE_API,
    LaunchError,
    LaunchRequest,
    Rejection,
    describe_choice,
    equivalent_command,
    parse_instrument_tokens,
    parse_launch_args,
    resolve_emulation,
    resolve_history,
)
from src.assembly.contour import ContourSettings, TradingContour, _family_warmup, _strategy_map
from src.assembly.core import CoreStarter, LiveCore, _say
from src.assembly.history import HistoricalMarketServices
from src.assembly.history_runs import HistoryRuns, clear_attempt
from src.assembly.core_state import CORE_DATABASE_FILE, CoreStateStore
from src.assembly.process import JournalMigrationError, contour_dir
from src.assembly.pulse import CorePulseWriter
from src.assembly import recovery as contour_recovery
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
from src.instruments import Instrument, normalize_instrument
from src.logging_setup import get_logger, setup_logging
from src.runtime_lock import RuntimeLockError, acquire_runtime_lock

log = get_logger(__name__)

_SECRETS = (TINKOFF_TOKEN, TELEGRAM_BOT_TOKEN, CLOUDFLARE_URL,
            MARKET_DATA_EXPORT_TOKEN, ROBOT_CONTROL_TOKEN, MARKET_STRUCTURE_REDIS_PASSWORD)


def main(argv=None):
    """Точка входа: служебный вход процесса контура или робот без параметров (add-history-contour)."""
    argv = list(argv or [])
    if argv[:1] == ["--contour"]:
        # Служебный вход процесса контура: его запускает робот (add-contour-process).
        return _run_contour(argv[1] if len(argv) > 1 else "", argv[2:])
    try:
        parse_launch_args(argv)
    except LaunchError as exc:
        print(str(exc))
        return 2
    return _run_robot()


def _run_contour(kind: str, args=()) -> int:
    """Процесс торгового контура: блокировка и журнал в своей папке, файл запуска, лента ядра (D1)."""
    import signal

    from src.assembly.contracts import contracts_from_payload, instrument_from_payload
    from src.assembly.contour_control import STATUS_FILE, ContourControl
    from src.config import DATABASE_FILE
    from src.assembly.feed import FeedMarketServices
    from src.assembly.process import LaunchFileError, StdinStop, read_launch
    from src.assembly.pulse import CorePulse
    from src.bot.run_session import StoppableRunSession
    from src.data.market_store import MarketDataStore

    if kind == MODE_HISTORY:
        return _run_history_contour(args[0] if args else "")
    if kind != MODE_EMULATION:
        print(f"Неизвестный вид контура {kind!r}: поддерживаются {MODE_EMULATION} и {MODE_HISTORY}.")
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
        secrets=_SECRETS,
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
                             request_stop=stop.request, version=__version__,
                             warm_timeout=MARKET_DATA_SETTINGS.startup_wait_timeout_secs)
    # Пульс ядра (add-contour-recovery): без него — запрет входов, «остановлено» и чужая версия — выход.
    pulse = CorePulse(data_dir / CORE_DATABASE_FILE, version=__version__, interval=CONTOURS_HEARTBEAT_SECS,
                      timeout=CONTOURS_HEARTBEAT_TIMEOUT_SECS, on_stop=stop.request, on_check=control.touch)
    pulse.start()
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
        pulse.close()
        # Штатный выход — отметка для ядра, подхватившего процесс без кода возврата.
        control.finish(stop.reason or "цикл завершён")
    finally:
        pulse.close()
        services.close()
        contour.close()
    log.info("Контур %s остановлен: %s", kind, stop.reason or "цикл завершён")
    return 0


def _run_history_contour(name: str) -> int:
    """Процесс прогона истории (add-history-contour): папка прогона, снимок от робота, прежний путь истории."""
    import signal

    from src.assembly.history_runs import RUN_PREFIX, HistoryProgress, history_dir
    from src.assembly.process import LaunchFileError, StdinStop, read_launch
    from src.bot.launch import parse_moment

    data_dir = runtime_dir()
    directory = history_dir(data_dir) / name
    if not name or not directory.is_dir():
        print(f"Нет папки прогона истории {directory}: прогон запускает робот командой API.")
        return 2
    try:
        acquire_runtime_lock(directory)
    except RuntimeLockError as exc:
        print(str(exc))
        return 1
    setup_logging(service_uid=LOGGING_SERVICE_UID, log_file=str(directory / LOGGING_FILE), secrets=_SECRETS,
                  **_log_settings(True, None))
    run_id = RUN_PREFIX + name
    log.info("Прогон %s v%s запущен", run_id, __version__)
    try:
        launch = read_launch(directory, run_id)
    except LaunchFileError as exc:
        log.error("%s", exc)
        print(str(exc))
        return 2
    stop = StdinStop()
    stop.start()
    # Ctrl+C в консоли доходит и до прогона: отчёт пишется после тика, а не обрывается.
    signal.signal(signal.SIGINT, lambda *_: stop.request("Ctrl+C"))
    progress = HistoryProgress(directory, run_id=run_id, core_db=data_dir / CORE_DATABASE_FILE, version=__version__,
                               interval=CONTOURS_HEARTBEAT_SECS, stop_event=stop.requested)
    progress.start()
    provider = EmulatorClientProvider()
    provider.use_snapshot(launch.get("snapshot_generation"))
    sync = MarketDataSync(**(launch.get("market_data_sync") or {}))
    request = LaunchRequest(MODE_HISTORY, SOURCE_API, tuple(launch["instruments"]),
                            start=parse_moment(launch["from"]), end=parse_moment(launch["to"]),
                            pause=float(launch["pause"]))
    code = 1
    try:
        code = _run_history(request.start, request.end, request.pause, request, state_dir=directory,
                            provider=provider, market_data_sync=sync, progress=progress)
    except Exception as exc:
        log.exception("Прогон %s завершился сбоем", run_id)
        progress.fail(f"сбой прогона: {type(exc).__name__}")
        code = 1
    finally:
        progress.finish(code)
    log.info("Прогон %s завершён с кодом %s", run_id, code)
    return code


def _run_robot(state_dir: Path | None = None, client_provider=None) -> int:
    """Робот без параметров: API управления и очередь истории, затем ядро с повтором старта (add-history-contour)."""
    state_dir = Path(state_dir or runtime_dir())
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        acquire_runtime_lock(state_dir)
    except RuntimeLockError as exc:
        print(str(exc))
        return 1
    setup_logging(service_uid=LOGGING_SERVICE_UID, log_file=str(state_dir / LOGGING_FILE), secrets=_SECRETS,
                  **_log_settings(False, None))
    log.info("Робот v%s запущен", __version__)
    if not ROBOT_CONTROL_TOKEN:
        _say("Робот не запущен: в .env не задан ROBOT_CONTROL_TOKEN — без ключа управления роботом нельзя "
             "управлять (эмуляция и история запускаются командами API).")
        return 2
    core_state = CoreStateStore(state_dir / CORE_DATABASE_FILE)
    # Пульс — до любого решения о контурах: свежий контур не должен прочитать «остановлено» прошлого робота.
    pulse = CorePulseWriter(core_state, pid=os.getpid(), version=__version__, interval=CONTOURS_HEARTBEAT_SECS)
    pulse.start()
    control = LaunchControl(core_state=core_state, contour_dir=contour_dir(state_dir, MODE_EMULATION),
                            log_file=LOGGING_FILE)
    control.accept_commands()
    history = HistoryRuns(core_state, state_dir, max_runs=CONTOURS_MAX_HISTORY_RUNS, version=__version__,
                          heartbeat_timeout=CONTOURS_HEARTBEAT_TIMEOUT_SECS, share_strategies=SHARE_STRATEGIES,
                          future_strategies=FUTURE_STRATEGIES, log_file=LOGGING_FILE,
                          provider_factory=EmulatorClientProvider, say=_say)
    history.recover()
    control.history = history
    core = LiveCore(state_dir, client_provider)
    core.pulse = pulse
    core.starter = CoreStarter(core, token_required=client_provider is None)
    control.core_status = core.core_snapshot
    control.contour_busy = lambda: _contour_busy(state_dir)
    try:
        server = core.start_control_api(control)
    except OSError as exc:
        _say(f"Робот не запущен: API управления не поднялся на {MARKET_DATA_EXPORT_HOST}:{MARKET_DATA_EXPORT_PORT} "
             f"({type(exc).__name__}) — порт занят другим процессом?")
        pulse.stop()
        return 2
    host, port = server.address
    _say(f"Робот запущен, API: http://{host}:{port}")
    history.start()
    code = 0
    try:
        if core.starter.run():
            code = _serve_emulation(state_dir, core, core_state, control, client_provider)
        else:
            _wait_for_ctrl_c()
    except KeyboardInterrupt:
        log.info("Робот останавливается по Ctrl+C")
    finally:
        # «Остановлено» в пульсе — раньше ожидания прогонов: подхваченные прогоны узнают об остановке из него.
        pulse.stop()
        history.close()
        core.close()
    return code


def _wait_for_ctrl_c() -> None:
    """Ядро не стартует без токена брокера: робот держит API и историю до Ctrl+C."""
    import time

    while True:
        time.sleep(1)


def _contour_busy(state_dir) -> int | None:
    """pid процесса, который держит папку эмуляции и не подтверждает себя контуром; иначе ``None``."""
    plan = contour_recovery.plan_recovery(
        contour_dir(state_dir, MODE_EMULATION), version=__version__, desired=None, explicit=True, autostart=False,
        timeout=CONTOURS_HEARTBEAT_TIMEOUT_SECS)
    return plan.pid if plan.action == contour_recovery.BLOCKED else None


def _serve_emulation(state_dir, core, core_state, control, client_provider) -> int:
    """Ядро готово: судьба контура эмуляции (подхват, автоподъём) или ожидание команды, затем надзор."""
    budget = None if core.early is None else core.early.request_budget
    recovery = _recover_contour(state_dir, core_state, control)
    if recovery.action in (contour_recovery.ADOPT, contour_recovery.AUTOSTART):
        instruments = recovery.instruments()
    else:
        recovery = None
        instruments = [normalize_instrument(item)
                       for item in _await_command(control, core.export_server, client_provider, budget, None)]
    core.ensure_market_data()

    def next_launch():
        """После выхода контура — следующая команда API (повторный запуск)."""
        chosen = _await_command(control, core.export_server, client_provider,
                                None if core.early is None else core.early.request_budget, None)
        snapshot = control.snapshot()
        return [normalize_instrument(item) for item in chosen], SOURCE_API, snapshot.get("command") or ""

    def resolve_tokens(tokens):
        """Токены команды состава — по правилам запуска, в главном потоке ядра (add-contour-membership)."""
        request = LaunchRequest(MODE_EMULATION, SOURCE_API, parse_instrument_tokens(tokens))
        return _resolve_request(request, client_provider,
                                None if core.early is None else core.early.request_budget, None, history=False)

    try:
        return core.run_contour(instruments, kind=MODE_EMULATION, control=control, core_state=core_state,
                                source=SOURCE_API, command=control.snapshot().get("command") or "",
                                next_launch=next_launch, resolve=resolve_tokens, recovery=recovery)
    except JournalMigrationError as exc:
        print(str(exc))
        return 2


def _run_history(start, end, pause, request: LaunchRequest | None = None, *, state_dir, provider,
                 market_data_sync: MarketDataSync | None = None, progress=None, instruments=None):
    """Прогон с повтором, если проверка сдвинула начало на первую доступную свечу.

    Сдвиг меняет часы, поэтому прогон собирается заново в той же папке: файлы
    журнала прошлой попытки удаляются, запуск, статус и логи остаются (add-history-contour).
    Уже выбранные инструменты передаются в повтор. Повтор ровно один и идёт
    на том же снимке эмулятора — сверку импорта сделал робот до старта.
    """
    for _ in range(2):
        clock = HistoricalClock(start, end, smallest_period(_timeframes()), pause)
        session = build_run_session(MODE_HISTORY, clock)
        if progress is not None:
            progress.attach(clock, session)
        code, shifted, instruments = _launch(
            state_dir=state_dir,
            client_provider=provider,
            clock=clock,
            session=session,
            channel_names=[],
            history=True,
            instruments=instruments,
            market_data_sync=market_data_sync,
            launch=request,
            progress=progress,
        )
        if shifted is None:
            return code
        clear_attempt(state_dir, LOGGING_FILE)
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
    history: bool = True,
    instruments=None,
    market_data_sync: MarketDataSync | None = None,
    launch: LaunchRequest | None = None,
    progress=None,
) -> tuple[int, object, list]:
    """Сборка исторического прогона: историческая служба данных, затем торговый контур поверх неё.

    Контур (``TradingContour``) тот же, что у эмуляции, поэтому исторический
    прогон идёт по тому же конвейеру. Эмуляцию с add-history-contour собирает
    ``_run_robot``; ``history`` остаётся для совместимости вызовов.

    Возвращает код завершения, сдвиг начала и выбранные инструменты: если
    проверка перенесла начало на первую доступную свечу, собранные часы уже не
    годятся, а прогонять диапазон должен вызывающий — он же передаст сюда
    ``instruments``, чтобы не разрешать их повторно. ``progress`` —
    ``HistoryProgress`` процесса прогона: фазы, отказы и причина для API.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(
        service_uid=LOGGING_SERVICE_UID,
        log_file=str(state_dir / LOGGING_FILE),
        secrets=_SECRETS,
        **_log_settings(True, clock),
    )
    log.info("Робот v%s запущен", __version__)
    if instruments is None:
        instruments = _history_instruments(launch, client_provider, clock, progress)
        if instruments is None:
            return 2, None, None
    if not isinstance(instruments, HistorySelection):
        instruments = [item if isinstance(item, Instrument) else normalize_instrument(item) for item in instruments]

    selection = instruments if isinstance(instruments, HistorySelection) else None
    if selection is not None:
        try:
            selection = selection.planned(session.start, session.end, HISTORICAL_ROLLOVER, CONTRACT_EXPIRY_BLOCK_DAYS)
        except ValueError as exc:
            return _refused(f"Прогон невозможен — контрактная цепочка: {exc}", progress), None, selection
        instruments = selection.instruments

    provider = client_provider
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
            return _refused(f"Прогон невозможен — контрактная цепочка: {exc}", progress), None, selection
        instruments = selection.instruments
        selection.save(state_dir)
        session.contract_selection = selection

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
        return _refused(report.message(), progress), None, selection or instruments
    if report.start != session.start:
        print(report.message())
        return 3, report.start, selection or instruments
    if report.notes:
        print(report.message())
    session.set_boundary_checks(report.boundaries)
    if selection is None:
        _preload_history_schedule(market_calendar, selection, instruments, session)

    services = HistoricalMarketServices(state_dir, clock, provider, market_calendar, report.boundaries)
    contour = TradingContour(ContourSettings(
        kind=MODE_HISTORY, state_dir=state_dir, channel_names=channel_names,
        clock=clock, session=session, client_provider=provider, instruments=instruments,
        market_data_sync=market_data_sync,
    ), services)
    try:
        contour.build()
        if progress is not None:
            progress.phase("running")
        contour.run(services.serve(contour.demand()))
    finally:
        if progress is not None:
            progress.phase("finishing")
        services.close()
        contour.finish()
        contour.close()
    return 0, None, selection or instruments


def _refused(message: str, progress) -> int:
    """Прогон отклонён до первого тика: причина в консоль прогона и в ``status.json``."""
    print(message)
    if progress is not None:
        progress.fail(message.strip().splitlines()[0] if message.strip() else "прогон отклонён")
    return 2


def _history_instruments(launch, client_provider, clock, progress):
    """Инструменты прогона из токенов команды: семейства — цепочкой, акции и контракты — фиксированно."""
    if launch is None or launch.instruments is None:
        _refused("Инструменты прогона не заданы.", progress)
        return None
    resolution = _resolve_request(launch, client_provider, None, clock.now(), history=True)
    _print_resolution(resolution)
    if not resolution.ok:
        print("Прогон не запущен: инструменты отклонены.")
        if progress is not None:
            progress.reject(resolution.rejections or [Rejection("*", "Не выбрано ни одного инструмента.")])
        return None
    return resolution.instruments


def _recover_contour(state_dir, core_state, control):
    """Судьба контура эмуляции, когда ядро готово: подхват, замена другой версии, автоподъём (add-contour-recovery).

    Явного запуска при старте нет (add-history-contour): робот всегда стартует в ожидании команды.
    """
    from src.assembly.process import STOP_TIMEOUT_SECS, terminate_pid, wait_exit

    directory = contour_dir(state_dir, MODE_EMULATION)

    def plan():
        return contour_recovery.plan_recovery(
            directory, version=__version__, desired=core_state.desired(MODE_EMULATION), explicit=False,
            autostart=CONTOURS_AUTOSTART, timeout=CONTOURS_HEARTBEAT_TIMEOUT_SECS)

    result = plan()
    if result.action == contour_recovery.REPLACE:
        if result.status.get("version") != __version__:
            _say(f"Контур другой версии ({result.status.get('version')}, pid {result.pid}) завершается: "
                 f"робот {__version__} поднимет контур своей версии")
        else:
            _say(f"Контур (pid {result.pid}) ещё завершается — робот ждёт его выхода")
        # Контур увидит пульс чужой версии и выйдет после тика; иначе — принудительно.
        if not wait_exit(result.pid, STOP_TIMEOUT_SECS + CONTOURS_HEARTBEAT_SECS):
            log.error("Контур pid %s не вышел за %.0f с — завершается принудительно", result.pid, STOP_TIMEOUT_SECS)
            terminate_pid(result.pid)
            wait_exit(result.pid, 5)
        result = plan()
        if result.action in (contour_recovery.ADOPT, contour_recovery.REPLACE):
            result = contour_recovery.RecoveryPlan(contour_recovery.BLOCKED, result.pid, result.status)
    desired = core_state.desired(MODE_EMULATION)
    if result.action == contour_recovery.BLOCKED:
        message = (f"Папка контура {directory} занята процессом pid {result.pid}, который не подтверждает себя "
                   f"как контур робота. Завершите этот процесс; если такого процесса робота нет — удалите "
                   f"{directory / 'robot.pid'}. Эмуляция не запускается, пока папка занята; API и история работают.")
        print(message)
        log.error("%s", message)
    elif result.action == contour_recovery.ADOPT:
        _say(f"Контур эмуляции подхвачен: pid {result.pid}, поколение {desired.generation if desired else '—'}")
        control.mark_recovered(contour_recovery.RECOVERY_ADOPTED, desired)
    elif result.action == contour_recovery.AUTOSTART:
        names = ", ".join(member.name for member in result.members)
        _say(f"Контур эмуляции поднимается без команды: {names}, поколение {desired.generation}")
        control.mark_recovered(contour_recovery.RECOVERY_AUTOSTART, desired)
    return result



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
    print(f"Повторить запуск: POST /api/v1/contours {command}")
    log.info("Повторить запуск: POST /api/v1/contours %s", command)
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
