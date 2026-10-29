"""Поток ``OrderStateStream`` контура на брокере (add-tinvest-orders, design.md решение 7).

Поток только пишет журнал заявок через ``TInvestBroker.ingest``: ``trades.sqlite3``
он не трогает, ``consume`` вызывает главный поток. Обрыв — переподключение с
паузой 1, 2, 4… до 30 с; после каждого подключения незавершённые заявки
досверяются по ключу (``resync``), чтобы не потерять сделки, пришедшие без стрима.
"""

from __future__ import annotations

import threading

from src.broker_contour.api import OrderFailure
from src.logging_setup import get_logger
from src.sandbox.gateway import BrokerCallError

log = get_logger(__name__)

BACKOFF_SECS = (1, 2, 4, 8, 16, 30)


class OrderStream:
    def __init__(self, api, broker, *, stop: threading.Event | None = None, backoff=BACKOFF_SECS):
        self._api = api
        self._broker = broker
        self._stop = stop or threading.Event()
        self._backoff = backoff
        self._thread: threading.Thread | None = None
        self.foreign = 0
        self.connects = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="order-stream", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                for report in self._api.order_stream():
                    if self._stop.is_set():
                        return
                    if failures or not self.connects:
                        self._connected()
                        failures = 0
                    if report is None:
                        continue
                    if not self._broker.ingest(report):
                        self.foreign += 1
                        log.debug("Стрим: событие по чужой заявке пропущено")
                log.warning("Стрим заявок закрыт брокером")
            except (BrokerCallError, OrderFailure) as exc:
                log.warning("Стрим заявок оборвался (%s)", getattr(exc, "code", type(exc).__name__))
            except Exception as exc:  # noqa: BLE001 — поток не должен умирать молча
                log.warning("Стрим заявок оборвался (%s)", type(exc).__name__)
            if self._stop.is_set():
                return
            self._broker.stream_link = "lost"
            delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            self._stop.wait(delay)

    def _connected(self) -> None:
        self.connects += 1
        self._broker.stream_link = "ok"
        if self.connects > 1:
            log.info("Стрим заявок восстановлен: досверка незавершённых заявок по ключу")
            try:
                self._broker.resync()
            except Exception as exc:  # noqa: BLE001
                log.warning("Досверка после переподключения не удалась (%s)", type(exc).__name__)
