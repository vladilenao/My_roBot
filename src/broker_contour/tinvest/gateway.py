"""Шлюзы к Т-Инвест: единственная точка входа контура и команды в SDK брокера.

``SandboxGateway`` открывает ``SandboxClient`` только на адресе песочницы с
токеном песочницы, ``AccountGateway`` (add-account-contour) — обычный ``Client``
на адресе брокера с токеном счёта и не пускает вызовы песочницы. Оба ставят
срок каждому запросу и пишут каждый вызов в записи.
Ошибку брокера он превращает в ``BrokerCallError`` с кодом gRPC и описанием
брокера, не пропуская наружу объект исключения SDK целиком.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import grpc
from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
from t_tech.invest.exceptions import RequestError
from t_tech.invest.sandbox.client import SandboxClient

from src.api.client import disable_broker_telemetry
from src.api.deadline import RpcDeadline
from src.broker_contour.port import BrokerCallError
from src.logging_setup import get_logger
from src.sandbox.records import CallRecorder

log = get_logger(__name__)

SANDBOX_TARGET = INVEST_GRPC_API_SANDBOX
ACCOUNT_TARGET = INVEST_GRPC_API
SANDBOX_ONLY = "SANDBOX_ONLY"
REFERENCE_ONLY = "REFERENCE_ONLY"     # справочник ядра: только instruments.* (add-finam-contour)
DEADLINE_SECS = 30
# Песочница отвечает RESOURCE_EXHAUSTED и на частые заявки подряд, не только по
# минутному лимиту (разведка 29.10: третья заявка за 200 мс). Повтор с тем же
# ключом безопасен: отказ по лимиту приходит до приёма заявки.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECS = (1, 2, 4)
RATE_LIMIT_MAX_WAIT_SECS = 10


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


def _account_client(token: str):
    disable_broker_telemetry()
    return Client(token, target=ACCOUNT_TARGET, interceptors=[RpcDeadline(DEADLINE_SECS)])


class BrokerGateway:
    """Сервисы клиента Т-Инвест за вызовом ``call("orders.post_order", ...)``; вид задаёт подкласс."""

    LABEL = "Брокер"
    TOKEN_ENV = "токен брокера"
    _default_factory = staticmethod(_default_client)

    def __init__(self, token: str, *, client_factory: Callable[[str], Any] | None = None,
                 recorder: CallRecorder | None = None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if not token:
            raise ValueError(f"Не задан {self.TOKEN_ENV}")
        self._token = token
        self._client_factory = client_factory or self._default_factory
        self._client = None
        self._services = None
        self.recorder = recorder
        self._monotonic = monotonic
        self._sleep = sleep

    def __enter__(self) -> "BrokerGateway":
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

    def _check(self, method: str) -> None:
        """Вызов, недопустимый для вида шлюза, отклоняется до сети."""

    def call(self, method: str, *args, **kwargs):
        """Вызов сервиса; ``RESOURCE_EXHAUSTED`` повторяется до ``RATE_LIMIT_RETRIES`` раз."""
        self._check(method)
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            started = self._monotonic()
            try:
                response = self._method(method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — любой сбой вызова становится BrokerCallError
                error = broker_error(method, exc)
                self._record(method, args, kwargs, started, error=error)
                if error.code == "RESOURCE_EXHAUSTED" and attempt < RATE_LIMIT_RETRIES:
                    delay = _rate_limit_wait(exc, attempt)
                    log.warning("%s: %s — лимит запросов, повтор %d из %d через %g с", self.LABEL,
                                method, attempt + 1, RATE_LIMIT_RETRIES, delay)
                    self._sleep(delay)
                    continue
                log.warning("%s: %s — %s", self.LABEL, method, error)
                raise error from None
            self._record(method, args, kwargs, started, response=response)
            log.debug("%s: %s выполнен", self.LABEL, method)
            return response
        raise AssertionError("недостижимо")

    def stream(self, method: str, **kwargs) -> Iterator[Any]:
        """Сообщения стрима; каждое пишется в записи, сбой — ``BrokerCallError``."""
        self._check(method)
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


class SandboxGateway(BrokerGateway):
    """Песочница Т-Инвест: ``SandboxClient`` на адресе песочницы, токен ``TINKOFF_SANDBOX_TOKEN``."""

    LABEL = "Песочница"
    TOKEN_ENV = "TINKOFF_SANDBOX_TOKEN"
    _default_factory = staticmethod(_default_client)


class AccountGateway(BrokerGateway):
    """Брокерский счёт робота (add-account-contour): ``Client`` на адресе брокера, токен ``TINKOFF_TRADE_TOKEN``.

    Сервис песочницы (``sandbox.*``) недоступен: вызов отклоняется до сети кодом ``SANDBOX_ONLY``.
    """

    LABEL = "Счёт"
    TOKEN_ENV = "TINKOFF_TRADE_TOKEN"
    _default_factory = staticmethod(_account_client)

    def _check(self, method: str) -> None:
        if method.split(".", 1)[0] == "sandbox":
            error = BrokerCallError(method, SANDBOX_ONLY, "вызов песочницы недоступен на счёте")
            self._record(method, (), {}, self._monotonic(), error=error)
            raise error


class ReferenceGateway(BrokerGateway):
    """Справочник Т-Инвест на токене ядра (add-finam-contour, решение 4): только ``instruments.*``.

    Карточки ядра для контура на другом брокере; любой другой вызов отклоняется до сети.
    """

    LABEL = "Справочник Т-Инвест"
    TOKEN_ENV = "TINKOFF_TOKEN"
    _default_factory = staticmethod(_account_client)

    def _check(self, method: str) -> None:
        if method.split(".", 1)[0] != "instruments":
            raise BrokerCallError(method, REFERENCE_ONLY, "справочнику доступно только instruments.*")
