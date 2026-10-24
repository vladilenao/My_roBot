"""Ядро эмуляции: рыночная база, арбитр, календарь, полосы, приём свечей, локальный API, кластерный поток."""

import os



from src.config import (
    CATCH_UP_BARS,
    DATA_BACKFILL_WINDOW_SECONDS,
    DATA_REFRESH_MIN_INTERVAL,
    MARKET_DATA_DATABASE_FILE,
    MARKET_DATA_EXPORT_ENABLED,
    MARKET_DATA_EXPORT_HOST,
    MARKET_DATA_EXPORT_PORT,
    MARKET_DATA_SETTINGS,
    MARKET_DATA_EXPORT_TOKEN,
    ROBOT_CONTROL_TOKEN,
    MARKET_STRUCTURE_PATTERNS_ENABLED,
    MARKET_STRUCTURE_QUEUE_MAXSIZE,
    MARKET_STRUCTURE_REDIS_DB,
    MARKET_STRUCTURE_REDIS_HOST,
    MARKET_STRUCTURE_REDIS_PASSWORD,
    MARKET_STRUCTURE_REDIS_PORT,
    MARKET_STRUCTURE_RETENTION_DAYS,
    FUTURE_STRATEGIES,
    SHARE_STRATEGIES,
    SLEEP_SECONDS,
    TICK_POLL_SECS,
    TINKOFF_TOKEN,
    DATABASE_FILE,
    CLUSTER_SETTINGS,
    CLUSTERS_ENABLED,
    CONTOURS_FEED_POLL_SECS,
    CONTOUR_EMULATION_TELEGRAM_CHANNEL_ENV,
    trading_enabled,
)
from src.api.client import default_client_provider
from src.data.cache import MarketDataCache
from src.data.loader import load_candles
from src.data.market_store import SOURCE_TBANK, MarketDataStore
from src.data.export_api import MarketDataExportServer
from src.data.reconciliation import MarketDataReconciler
from src.data.poller import LiveMarketDataPoller
from src.data.ingestion import IngestionWorker
from src.market_structure.trigger import PatternTrigger
from src.data.lanes import CalendarLane, ChangesRetentionLane, CompletenessLane, EvidenceLane
from src.data.stream_ingestion import StreamSupervisor
from src.instruments import Instrument
from src.logging_setup import get_logger
from src.scheduler.timing import MultiTimeframeScheduler


from src.assembly.services import ContourDemand, _timeframes
from src.assembly.contracts import _load_contracts_metadata, _pattern_pairs, contracts_payload, instrument_payload

log = get_logger(__name__)


def _configured_cluster_mode() -> str:
    """Режим «Кластеров» по всем привязкам: один общий или mixed."""
    modes = {assignment.clusters for items in (*SHARE_STRATEGIES.values(), *FUTURE_STRATEGIES.values())
             for assignment in items}
    return modes.pop() if len(modes) == 1 else "mixed"


def _start_local_api(market_store, state_dir, control=None):
    """Локальный HTTP API: экспорт, отчёт кластерного фильтра и управление контуром.

    Сервер поднимается, если задан хотя бы один ключ; маршруты без своего ключа
    отвечают 401. Без ключей API не запускается, а робот работает дальше:
    ошибка конфигурации доступа не должна останавливать торговлю.
    """
    from src.clusters.report import ClusterReport

    control_token = ROBOT_CONTROL_TOKEN if control is not None else None
    if not MARKET_DATA_EXPORT_TOKEN and not control_token:
        _say("Локальный API не запущен: в .env не задан MARKET_DATA_EXPORT_TOKEN")
        _say("Управление по API недоступно: в .env не задан ROBOT_CONTROL_TOKEN")
        return None
    if not MARKET_DATA_EXPORT_TOKEN:
        _say("Экспорт и отчёт кластеров недоступны: в .env не задан MARKET_DATA_EXPORT_TOKEN")
    if not control_token:
        _say("Управление по API недоступно: в .env не задан ROBOT_CONTROL_TOKEN")
    from src.assembly.process import contour_dir

    # Журнал эмуляции живёт в папке контура (add-contour-process, D7).
    report = ClusterReport(
        contour_dir(state_dir, "emulation") / DATABASE_FILE,
        lambda uid: market_store.current_frame(source=SOURCE_TBANK, instrument_uid=uid, interval="1m"),
        configured_mode=_configured_cluster_mode(),
    )
    server = MarketDataExportServer(
        market_store, MARKET_DATA_EXPORT_TOKEN,
        host=MARKET_DATA_EXPORT_HOST, port=MARKET_DATA_EXPORT_PORT, clusters_report=report,
        control=control, control_token=control_token,
    )
    server.start()
    return server


def _after(stamp: str, moment) -> bool:
    """Подтверждение курсора сделано после ``moment`` (UTC)."""
    import pandas as pd

    value = pd.Timestamp(stamp)
    value = value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")
    return value.to_pydatetime() >= moment


def _say(message: str) -> None:
    log.warning(message)
    print(message)


def _cluster_redis():
    import redis

    return redis.Redis(
        host=MARKET_STRUCTURE_REDIS_HOST,
        port=MARKET_STRUCTURE_REDIS_PORT,
        db=MARKET_STRUCTURE_REDIS_DB,
        password=MARKET_STRUCTURE_REDIS_PASSWORD,
        decode_responses=True,
        socket_timeout=10,
    )


def _live_cluster_gate(*, market_store, data_cache, state_dir):
    """Кластерный фильтр эмуляции: фоновый потребитель Redis и вердикт по 1m свечам базы."""
    if not CLUSTERS_ENABLED:
        return None, None
    from src.clusters.consumer import ClusterEventsConsumer
    from src.clusters.gate import ClusterGate, store_candles
    from src.clusters.store import FILE_NAME, ClusterEventStore

    store = ClusterEventStore(state_dir / FILE_NAME)
    consumer = ClusterEventsConsumer(store, _cluster_redis(), stream=CLUSTER_SETTINGS.stream,
                                     group=CLUSTER_SETTINGS.consumer_group)
    consumer.start()
    # Инструменты live-выбора без UID: адрес пары свечей знает кэш (pair_uid).
    gate = ClusterGate(store, store_candles(market_store, SOURCE_TBANK, data_cache.pair_uid), CLUSTER_SETTINGS,
                       consumer=consumer, live=True, uid_for=data_cache.pair_uid)
    return gate, consumer


def _build_market_data(*, state_dir, clock, provider, history, market_calendar=None):
    """Часы, рыночная база, арбитр, календарь, сверка и владелец входа."""
    settings = MARKET_DATA_SETTINGS
    lanes = not history and settings.independent_lanes
    timeline = MultiTimeframeScheduler(
        timeframes=_timeframes(),
        sleep_secs=SLEEP_SECONDS if clock is None else clock.pause_secs,
        catch_up_bars=CATCH_UP_BARS,
        clock=clock,
    )
    market_store = None if history else MarketDataStore(state_dir / MARKET_DATA_DATABASE_FILE)
    if market_store is not None:
        # Готовность прошлого запуска не действует: пары заново проходят прогрев.
        market_store.drop_pair_readiness(source=SOURCE_TBANK)
    from src.data.request_budget import RequestBudget
    # Полосы: без глобального окна темпа (свежий опрос ограничен для пары),
    # раздельные сервисы и доли полос (design 2).
    request_budget = None if history else RequestBudget(
        timeline.now, 0 if lanes else DATA_REFRESH_MIN_INTERVAL,
        per_minute=settings.unary_requests_per_minute,
        per_minute_by_service={"instruments": settings.instrument_requests_per_minute if lanes else 200},
        lane_shares=settings.lane_shares() if lanes else None)
    if market_store is not None:
        from src.market_calendar.service import MarketCalendar

        market_calendar = MarketCalendar(
            market_store,
            provider.calendar_source(
                market_store, token=TINKOFF_TOKEN, request_budget=request_budget,
                horizon_days=MARKET_DATA_SETTINGS.schedule_horizon_days,
                refresh_seconds=MARKET_DATA_SETTINGS.schedule_refresh_interval_secs,
            ),
            schedule_max_age_secs=MARKET_DATA_SETTINGS.schedule_max_age_secs,
        )
    market_reconciler = None if market_store is None else MarketDataReconciler(
        market_store, source="tbank_exchange", token=TINKOFF_TOKEN,
        client_provider=provider,
        request_budget=request_budget,
        market_calendar=market_calendar,
        calendar_evidence=MARKET_DATA_SETTINGS.calendar_evidence,
        candle_publication_timeout_secs=MARKET_DATA_SETTINGS.candle_publication_timeout_secs,
        candle_publication_timeout_by_tf=MARKET_DATA_SETTINGS.candle_publication_timeout_by_tf,
    )
    data_cache = MarketDataCache(
        loader=load_candles,
        timeline=timeline,
        token=TINKOFF_TOKEN,
        data_refresh_min_interval=DATA_REFRESH_MIN_INTERVAL if clock is None else 0.0,
        data_backfill_window_seconds=None if clock is not None else DATA_BACKFILL_WINDOW_SECONDS,
        freshness_tolerance_bars=CATCH_UP_BARS,
        clock=clock,
        client_provider=provider,
        market_store=market_store,
        market_reconciler=market_reconciler,
        market_calendar=market_calendar,
        request_budget=request_budget,
        recovery_retry_seconds=MARKET_DATA_SETTINGS.recovery_recheck_interval_secs,
    )
    return timeline, market_store, request_budget, market_calendar, market_reconciler, data_cache


class _EarlyMarketData:
    """Слой данных боевого режима, собранный до выбора инструментов."""

    def __init__(self, **values):
        self.__dict__.update(values)


def _start_early_market_data(state_dir, provider):
    """Рыночная база, арбитр, календарь, владелец входа и фоновые полосы до выбора.

    Состав — сохранённые пары прошлого запуска и незавершённых сделок; граница
    запуска — момент старта процесса (design 9).
    """
    settings = MARKET_DATA_SETTINGS
    timeline, market_store, request_budget, market_calendar, market_reconciler, data_cache = _build_market_data(
        state_dir=state_dir, clock=None, provider=provider, history=False)
    log.info("Live-ingestion: эффективный профиль %s", settings.as_dict())
    for name in settings.deprecated_overrides():
        log.warning("Параметр [market_data] %s не используется при независимых полосах", name)
    worker = IngestionWorker(data_cache, settings=settings)
    completeness = CompletenessLane(worker)
    evidence = EvidenceLane(worker, completeness)
    worker.evidence = evidence
    worker.completeness = completeness
    lanes = [CalendarLane(market_reconciler, timeline), evidence, completeness]
    if settings.changes_retention_days:
        lanes.append(ChangesRetentionLane(market_store, settings.changes_retention_days))
    registered = _register_saved_roster(data_cache, market_store)
    poller = LiveMarketDataPoller(data_cache, _timeframes(), TICK_POLL_SECS, worker=worker, lanes=lanes,
                                  status_interval=settings.lane_status_log_interval_secs)
    poller.start_background()
    log.info("Полосы данных запущены до выбора инструментов: пар сохранённого состава %d", registered)
    return _EarlyMarketData(timeline=timeline, market_store=market_store, request_budget=request_budget,
                            market_calendar=market_calendar, market_reconciler=market_reconciler,
                            data_cache=data_cache, worker=worker, completeness=completeness, poller=poller)


def _register_saved_roster(data_cache, market_store):
    """Пары прошлого запуска регистрируются у владельца входа, без сети."""
    count = 0
    for entry in market_store.roster(source=data_cache._market_source):
        instrument = Instrument(entry["short_name"] or entry["ticker"], entry["ticker"], entry["instrument_type"],
                                entry["short_name"], entry["instrument_uid"])
        min_bars = max(int((entry["requirements"] or {}).get("min_bars", 1)), 1)
        try:
            data_cache.register_live_pair(instrument, entry["instrument_uid"], entry["interval"], min_bars)
            count += 1
        except ValueError as exc:
            log.warning("Пара сохранённого состава %s %s не зарегистрирована: %s",
                        entry["ticker"], entry["interval"], exc)
    return count


def _start_live_ingestion(data_cache, demand, client_provider, early=None):
    """Сеть live-данных принадлежит фоновым полосам; бот только читает кэш.

    Потребности (обязательства, запросы истории, границы исполнения) собирает
    контур главным потоком из своей финансовой базы и передаёт объектом
    ``ContourDemand``: ядро финансовую SQLite не открывает.
    Возвращает поллер и владельца входа.
    """
    settings = MARKET_DATA_SETTINGS
    if early is None:
        log.info("Live-ingestion: эффективный профиль %s", settings.as_dict())
        worker = IngestionWorker(data_cache, settings=settings)
    else:
        worker = early.worker
    instruments = list(demand.instruments)
    if demand.obligations is not None:
        worker.set_execution_obligations(demand.obligations)
        # Прежний порядок: по каждому инструменту запросы истории, затем граница исполнения.
        for instrument in instruments:
            for item, timeframe, count in demand.history_requests:
                if item is instrument:
                    data_cache.request_live_history(instrument, timeframe, count)
            for item, lower in demand.execution_lower:
                if item is instrument:
                    data_cache.note_execution_lower(instrument, lower)
    if early is not None:
        _retire_unselected_pairs(data_cache, instruments, worker)
        worker.roster_dirty = True
    stream = None
    if settings.live_transport == "stream":
        provider = client_provider or default_client_provider()
        factory = getattr(provider, "stream_session", None)
        if factory is None:
            log.warning("Live-stream недоступен у провайдера %s — используется polling", type(provider).__name__)
        else:
            stream = StreamSupervisor(data_cache, worker, lambda: factory(
                TINKOFF_TOKEN, control_queue_max_messages=settings.stream_control_queue_max_messages),
                settings=settings)
    if early is not None:
        poller = early.poller
        poller.attach_stream(stream)
    else:
        poller = LiveMarketDataPoller(data_cache, _timeframes(), TICK_POLL_SECS, worker=worker, stream=stream)
    poller.start()
    return poller, worker


def _retire_unselected_pairs(data_cache, instruments, worker):
    """Пары сохранённого состава, не выбранные сейчас и без обязательств, снимаются."""
    tickers = {instrument.ticker for instrument in instruments}
    uids = {instrument.uid for instrument in instruments if getattr(instrument, "uid", None)}
    for pair in data_cache.live_pairs():
        instrument = pair["instrument"]
        if pair["uid"] in uids or instrument.ticker in tickers:
            continue
        if worker.execution_boundary(pair["uid"]) is not None:
            continue
        if data_cache.unregister_live_pair(pair["uid"], pair["timeframe"]):
            log.info("Пара %s %s прошлого запуска не выбрана — снята с обслуживания",
                     instrument.ticker, pair["timeframe"])


def _live_startup_waiter(poller, *, timeout):
    """Ожидание прогрева перед стартовым обзором (retire-unrequired-roster-pairs).

    LiveAnalysis вызывает его сразу после регистрации потребностей стратегий,
    фильтров и сопровождения. Пары сохранённого состава, которые никто не
    запросил, сначала снимаются: их прогрев не начнётся никогда. Ожидание
    ограничено ``timeout``: неготовые пары выходят в обзор с причиной.
    """
    def wait():
        for ticker, timeframe in poller.retire_unrequested_pairs():
            log.info("Пара %s %s сохранённого состава не нужна ни стратегиям, ни сделкам — снята с обслуживания",
                     ticker, timeframe)
        if poller.wait_startup(timeout):
            return True
        log.warning("Прогрев не завершился за %d с, стартовый обзор выходит без него. Ждали: %s",
                    timeout, "; ".join(poller.startup_pending()) or "нет данных")
        return False
    return wait


class LiveCore:
    """Ядро эмуляции: рыночные данные, приём, локальный API и кластерный поток.

    Реализует ``MarketServices`` для контура эмуляции. Порядок запуска и
    остановки повторяет прежний ``_launch``: ранний сбор до выбора
    инструментов, приём и паттерны после сборки торговой части.
    """

    def __init__(self, state_dir, client_provider=None) -> None:
        self.state_dir = state_dir
        self.client_provider = client_provider if client_provider is not None else default_client_provider()
        self.early = None
        self.export_server = None
        self.timeline = self.market_store = self.request_budget = None
        self.market_calendar = self.market_reconciler = self.data_cache = None
        self.pattern_sink = None
        self._pattern_trigger = None
        self._poller = None
        self._worker = None
        self._cluster_consumer = None
        self._tracks = False

    def start_early(self):
        """Ранний сбор данных до выбора инструментов (независимые полосы)."""
        self.early = _start_early_market_data(self.state_dir, self.client_provider)
        self._adopt(self.early)
        return self.early

    def start_api(self, control=None):
        """Локальный API, как только есть рыночная база; иначе — после сборки данных."""
        if self.export_server is None and self.market_store is not None and MARKET_DATA_EXPORT_ENABLED:
            self.export_server = _start_local_api(self.market_store, self.state_dir, control=control)
        return self.export_server

    def ensure_market_data(self):
        """Без независимых полос данные собираются после выбора инструментов, как раньше."""
        if self.data_cache is None:
            (self.timeline, self.market_store, self.request_budget, self.market_calendar,
             self.market_reconciler, self.data_cache) = _build_market_data(
                state_dir=self.state_dir, clock=None, provider=self.client_provider, history=False)
        return self

    def _adopt(self, early) -> None:
        for name in ("timeline", "market_store", "request_budget", "market_calendar", "market_reconciler", "data_cache"):
            setattr(self, name, getattr(early, name, None))

    def start_clusters(self) -> None:
        """Читатель Redis кластеров — у ядра; контур только читает хранилище (D7)."""
        if self._cluster_consumer is None:
            _, self._cluster_consumer = _live_cluster_gate(
                market_store=self.market_store, data_cache=self.data_cache, state_dir=self.state_dir)

    def run_contour(self, instruments, *, kind: str, control=None, source: str = "", command: str = "",
                    core_state=None, next_launch=None, process_factory=None, poll_secs: float = 1.0) -> int:
        """Цикл ядра: прогрев один раз, затем запуск процесса контура и надзор (add-contour-process, add-control-api).

        После выхода процесса ``next_launch`` ждёт следующую команду запуска и
        возвращает ``(инструменты, источник, команда)``; контур запускается
        заново без перезапуска приёма данных. Без ``next_launch`` (или если она
        вернула ``None``) ядро, как прежде, ждёт Ctrl+C: приём данных и API
        работают, контур сам не перезапускается.
        """
        import time

        from src.assembly.demand import ContourDemandPoller

        tracks = trading_enabled()
        poller = ContourDemandPoller(self.market_store, self.data_cache, instruments=instruments,
                                     tracks_obligations=tracks, poll_seconds=CONTOURS_FEED_POLL_SECS)
        # Строки обязательств прошлого запуска действуют сразу: целостность до старта контура.
        demand = ContourDemand(tuple(instruments), obligations=poller.obligations() if tracks else None,
                               pattern_pairs=_pattern_pairs(instruments))
        waiter = self.serve(demand)
        poller._worker = self._worker
        poller.start()
        self.start_clusters()
        if waiter is not None:
            waiter()
        process = None
        try:
            while True:
                process = self._start_contour_process(
                    instruments, kind=kind, control=control, source=source, command=command,
                    core_state=core_state, process_factory=process_factory)
                self._supervise(process, kind=kind, control=control, poll_secs=poll_secs)
                launch = None if next_launch is None else next_launch()
                if launch is None:
                    while True:
                        time.sleep(poll_secs)
                instruments, source, command = launch
                # Новые инструменты повторного запуска: сопровождение и паттерны ядра.
                poller.add_instruments(instruments)
                if self._pattern_trigger is not None:
                    self._pattern_trigger.add_pairs(_pattern_pairs(instruments))
        except KeyboardInterrupt:
            log.info("Ядро останавливается: контур %s получает stop", kind)
        finally:
            if process is not None:
                process.stop()
            poller.stop()
        return 0 if process is None or process.exit_code in (0, None) else 1

    def _start_contour_process(self, instruments, *, kind, control, source, command, core_state, process_factory):
        """Переезд журнала, карточки контрактов, желаемое «работает», ``launch.json`` и процесс."""
        from src import __version__
        from src.assembly.process import ContourProcess, contour_dir, migrate_journal, write_launch

        directory = contour_dir(self.state_dir, kind)
        migrate_journal(self.state_dir, directory, version=__version__)
        contracts = _load_contracts_metadata(instruments, client_provider=self.client_provider,
                                             request_budget=self.request_budget)
        payloads = [instrument_payload(item) for item in instruments]
        # Желаемое — до запуска процесса: контур на старте не должен увидеть прежнюю остановку.
        generation = None if core_state is None else core_state.record_launch(
            kind, kind, payloads, source=source, command=command)
        write_launch(directory, {
            "contour_id": kind, "kind": kind, "core_pid": os.getpid(), "core_version": __version__,
            "source": source, "command": command, "generation": generation,
            "telegram_channel_env": CONTOUR_EMULATION_TELEGRAM_CHANNEL_ENV,
            "instruments": payloads,
            "contracts": contracts_payload(contracts),
        })

        def exited(code, stopping):
            if control is not None:
                control.mark_exited(code, stopping=stopping)

        factory = process_factory or ContourProcess
        process = factory(kind, on_exit=exited)
        pid = process.start()
        if control is not None:
            control.mark_process(pid)
        return process

    def _supervise(self, process, *, kind, control, poll_secs) -> None:
        """Пока процесс жив: первое подтверждение курсора ленты переводит контур в ``running``."""
        import time
        from datetime import datetime, timezone

        from src.data.market_store import CONTOUR_CONSUMER_PREFIX

        started = datetime.now(timezone.utc)
        consumer = CONTOUR_CONSUMER_PREFIX + kind
        while process.running:
            if control is not None:
                saved = self.market_store.consumer_checkpoint(consumer)
                if saved is not None and _after(saved["acknowledged_at"], started):
                    control.mark_contour_running()
            time.sleep(poll_secs)

    def cluster_gate(self, state_dir):
        gate, self._cluster_consumer = _live_cluster_gate(
            market_store=self.market_store, data_cache=self.data_cache, state_dir=state_dir)
        return gate

    def serve(self, demand: ContourDemand):
        """Потребности контура → приём, поток, ожидание прогрева, обработчик паттернов и его сторож."""
        self._poller, self._worker = _start_live_ingestion(
            self.data_cache, demand, self.client_provider, early=self.early)
        self._tracks = demand.obligations is not None
        waiter = _live_startup_waiter(self._poller, timeout=MARKET_DATA_SETTINGS.startup_wait_timeout_secs)
        if MARKET_STRUCTURE_PATTERNS_ENABLED:
            import redis
            from src.market_structure.worker import PatternWorker

            self.pattern_sink = PatternWorker(
                self.market_store,
                redis.Redis(
                    host=MARKET_STRUCTURE_REDIS_HOST,
                    port=MARKET_STRUCTURE_REDIS_PORT,
                    db=MARKET_STRUCTURE_REDIS_DB,
                    password=MARKET_STRUCTURE_REDIS_PASSWORD,
                    decode_responses=True,
                ),
                # Как прежде: восстановление по паттернам — только при финансовой базе контура.
                recovery_requester=self._worker.note_loss if demand.obligations is not None else None,
                queue_maxsize=MARKET_STRUCTURE_QUEUE_MAXSIZE,
                retention_days=MARKET_STRUCTURE_RETENTION_DAYS,
            )
            self.pattern_sink.start()
            # Закрытые бары пар привязок ставит ядро, не тик контура.
            self._pattern_trigger = PatternTrigger(self.data_cache, self.timeline, self.pattern_sink,
                                                   demand.pattern_pairs)
            self._pattern_trigger.start()
        return waiter

    @property
    def tracks_obligations(self) -> bool:
        """Владелец входа ведёт обязательства только при финансовой базе контура."""
        return self._worker is not None and self._tracks

    def update_obligations(self, obligations) -> None:
        if self._worker is not None and obligations is not None:
            self._worker.set_execution_obligations(obligations)

    def acknowledge_tick(self) -> None:
        """Контур на памяти ядра курсора ленты не держит."""
        return None

    def stop_patterns(self) -> None:
        if self._pattern_trigger is not None:
            self._pattern_trigger.stop()
            self._pattern_trigger = None
        if self.pattern_sink is not None:
            self.pattern_sink.stop()
            self.pattern_sink = None

    def close(self) -> None:
        """Прежний порядок: паттерны, поллер (или ранние полосы), кластерный поток, API."""
        self.stop_patterns()
        if self._poller is not None:
            self._poller.close()
        elif self.early is not None:
            poller = getattr(self.early, "poller", None)
            if poller is not None:
                poller.close()
        if self._cluster_consumer is not None:
            self._cluster_consumer.stop()
        if self.export_server is not None:
            self.export_server.close()
