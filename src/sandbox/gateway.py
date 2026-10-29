"""Шлюз к песочнице Т-Инвест: единственная точка входа команды в SDK брокера.

Шлюз открывает ``SandboxClient`` только на адресе песочницы с токеном
песочницы, ставит срок каждому запросу и пишет каждый вызов в записи разведки.
Ошибку брокера он превращает в ``BrokerCallError`` с кодом gRPC и описанием
брокера, не пропуская наружу объект исключения SDK целиком.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import grpc
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.exceptions import RequestError
from t_tech.invest.sandbox.client import SandboxClient

from src.api.client import disable_broker_telemetry
from src.api.deadline import RpcDeadline
from src.logging_setup import get_logger
from src.sandbox.records import CallRecorder

log = get_logger(__name__)

SANDBOX_TARGET = INVEST_GRPC_API_SANDBOX
DEADLINE_SECS = 30
# Песочница отвечает RESOURCE_EXHAUSTED и на частые заявки подряд, не только по
# минутному лимиту (разведка 29.10: третья заявка за 200 мс). Повтор с тем же
# ключом безопасен: отказ по лимиту приходит до приёма заявки.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECS = (1, 2, 4)
RATE_LIMIT_MAX_WAIT_SECS = 10


class BrokerCallError(Exception):
    """Отказ брокера или сбой сети: код gRPC и описание брокера."""

    def __init__(self, method: str, code: str, details: str = "", message: str = ""):
        super().__init__(method, code, details, message)
        self.method = method
        self.code = code
        self.details = details
        self.message = message

    def __str__(self) -> str:
        head = " ".join(part for part in (self.code, self.details) if part)
        return f"{head}: {self.message}" if self.message else head


def broker_error(method: str, exc: BaseException) -> BrokerCallError:
    """Исключение вызова → код и описание; сам объект исключения не печатается."""
    if isinstance(exc, RequestError):
        code = getattr(exc.code, "name", str(exc.code))
        message = getattr(exc.metadata, "message", "") or ""
        return BrokerCallError(method, code, str(exc.details or ""), str(message))
    if isinstance(exc, grpc.RpcError) and hasattr(exc, "code"):
        status = exc.code()
        return BrokerCallError(method, getattr(status, "name", str(status)))
    return BrokerCallError(method, type(exc).__name__)


def _rate_limit_wait(exc: BaseException, attempt: int) -> float:
    """Пауза до повтора: подсказка брокера ``ratelimit_reset`` или нарастающая пауза."""
    reset = getattr(getattr(exc, "metadata", None), "ratelimit_reset", None)
    if isinstance(reset, int) and reset > 0:
        return min(reset, RATE_LIMIT_MAX_WAIT_SECS)
    return RATE_LIMIT_BACKOFF_SECS[min(attempt, len(RATE_LIMIT_BACKOFF_SECS) - 1)]


def _default_client(token: str):
    disable_broker_telemetry()
    return SandboxClient(token, interceptors=[RpcDeadline(DEADLINE_SECS)])


class SandboxGateway:
    """Сервисы ``SandboxClient`` за вызовом ``call("orders.post_order", ...)``."""

    def __init__(self, token: str, *, client_factory: Callable[[str], Any] | None = None,
                 recorder: CallRecorder | None = None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if not token:
            raise ValueError("Не задан TINKOFF_SANDBOX_TOKEN")
        self._token = token
        self._client_factory = client_factory or _default_client
        self._client = None
        self._services = None
        self.recorder = recorder
        self._monotonic = monotonic
        self._sleep = sleep

    def __enter__(self) -> "SandboxGateway":
        self._client = self._client_factory(self._token)
        self._services = self._client.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        client, self._client, self._services = self._client, None, None
        if client is not None:
            client.__exit__(exc_type, exc, tb)
        return False

    def set_account(self, account_id: str) -> None:
        """Номер счёта, который записи разведки заменяют на ``sandbox-account``."""
        if self.recorder is not None:
            self.recorder.set_account(account_id)

    def _method(self, method: str):
        target = self._services
        for name in method.split("."):
            target = getattr(target, name)
        return target

    def call(self, method: str, *args, **kwargs):
        """Вызов сервиса; ``RESOURCE_EXHAUSTED`` повторяется до ``RATE_LIMIT_RETRIES`` раз."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            started = self._monotonic()
            try:
                response = self._method(method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — любой сбой вызова становится BrokerCallError
                error = broker_error(method, exc)
                self._record(method, args, kwargs, started, error=error)
                if error.code == "RESOURCE_EXHAUSTED" and attempt < RATE_LIMIT_RETRIES:
                    delay = _rate_limit_wait(exc, attempt)
                    log.warning("Песочница: %s — лимит запросов, повтор %d из %d через %g с",
                                method, attempt + 1, RATE_LIMIT_RETRIES, delay)
                    self._sleep(delay)
                    continue
                log.warning("Песочница: %s — %s", method, error)
                raise error from None
            self._record(method, args, kwargs, started, response=response)
            log.debug("Песочница: %s выполнен", method)
            return response
        raise AssertionError("недостижимо")

    def stream(self, method: str, **kwargs) -> Iterator[Any]:
        """Сообщения стрима; каждое пишется в записи, сбой — ``BrokerCallError``."""
        started = self._monotonic()
        try:
            for message in self._method(method)(**kwargs):
                self._record(method, (), kwargs, started, response=message)
                yield message
        except Exception as exc:  # noqa: BLE001
            error = broker_error(method, exc)
            self._record(method, (), kwargs, started, error=error)
            raise error from None

    def _record(self, method, args, kwargs, started, *, response=None, error=None) -> None:
        if self.recorder is None:
            return
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "request": {"args": list(args), **kwargs} if args else kwargs,
            "duration_ms": round((self._monotonic() - started) * 1000),
        }
        if error is not None:
            entry["error"] = {"code": error.code, "details": error.details, "message": error.message}
        else:
            entry["response"] = response
        self.recorder.write(entry)
