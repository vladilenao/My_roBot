"""Шлюз Finam Trade API — единственная точка входа команды в gRPC Финама (add-finam-probe, решение 2).

Один TLS-канал на ``api.finam.ru:443``; ``Auth`` по секрету при входе, JWT — в
метаданных ``authorization`` каждого вызова и обновляется, когда старше
``JWT_REFRESH_SECS`` (``probe-jwt`` обновление выключает). Каждый JWT сразу
регистрируется как секрет лога и в записях заменяется на ``jwt#N``.

``RESOURCE_EXHAUSTED`` повторяется только у методов чтения: повтор заявки —
предмет разведки, а не скрытое поведение шлюза. Ошибка вызова становится
``BrokerCallError`` с кодом и описанием брокера; объект исключения не печатается.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import grpc

from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.auth import auth_service_pb2, auth_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.marketdata import marketdata_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.metrics import usage_metrics_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2_grpc
from src.finam.records import CallRecorder, Scrubber, mask_account
from src.logging_setup import get_logger, register_secrets
from src.sandbox.gateway import BrokerCallError

log = get_logger(__name__)

TARGET = "api.finam.ru:443"
DEADLINE_SECS = 30
JWT_REFRESH_SECS = 600
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECS = (1, 2, 4)
RATE_LIMIT_MAX_WAIT_SECS = 10
PUSHBACK_KEY = "grpc-retry-pushback-ms"
# AuthService принимает секрет или JWT только в теле запроса: с заголовком authorization
# TokenDetails отвечает INVALID_ARGUMENT «Token is invalid or malformed» (прогон 23.11).
BODY_AUTH_SERVICE = "auth"
# Изменяющие вызовы не повторяются автоматически ни при каком ответе.
WRITE_METHODS = frozenset({"orders.PlaceOrder", "orders.CancelOrder", "orders.PlaceSLTPOrder"})

STUBS = {
    "auth": auth_service_pb2_grpc.AuthServiceStub,
    "accounts": accounts_service_pb2_grpc.AccountsServiceStub,
    "orders": orders_service_pb2_grpc.OrdersServiceStub,
    "assets": assets_service_pb2_grpc.AssetsServiceStub,
    "marketdata": marketdata_service_pb2_grpc.MarketDataServiceStub,
    "metrics": usage_metrics_service_pb2_grpc.UsageMetricsServiceStub,
}


def _secure_channel(target: str):
    return grpc.secure_channel(target, grpc.ssl_channel_credentials())


def _default_stubs(channel) -> dict[str, Any]:
    return {name: stub(channel) for name, stub in STUBS.items()}


def _metadata_keys(exc: BaseException) -> list[str]:
    try:
        return sorted({str(key) for key, _ in (exc.trailing_metadata() or ())})
    except Exception:  # noqa: BLE001 — метаданные не обязательны
        return []


def _pushback_secs(exc: BaseException) -> float | None:
    try:
        for key, value in exc.trailing_metadata() or ():
            if key == PUSHBACK_KEY:
                millis = int(value)
                return millis / 1000 if millis >= 0 else None
    except Exception:  # noqa: BLE001
        return None
    return None


def clean_details(details: str | None) -> str:
    """Описание ошибки Финама в одну строку.

    Финам пишет текст на двух языках через перевод страницы (``\x0c``) и повторяет код gRPC в начале
    (прогон 23.11: ``INVALID_ARGUMENT: [145]No enough coverage…\x0cНедостаток обеспечения…``).
    """
    text = str(details or "")
    for code in ("INVALID_ARGUMENT", "FAILED_PRECONDITION", "NOT_FOUND", "RESOURCE_EXHAUSTED", "ALREADY_EXISTS"):
        text = text.removeprefix(f"{code}: ")
    return " ".join(text.replace("\x0c", " / ").split())


@dataclass
class StreamHandle:
    """Стрим в потоке-демоне: сообщения с временем прихода, ошибка конца, отмена."""

    method: str
    messages: list = field(default_factory=list)  # (datetime UTC, сообщение)
    error: BrokerCallError | None = None
    closed: threading.Event = field(default_factory=threading.Event)
    closed_at: datetime | None = None
    _call: Any = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _arrived: "queue.Queue" = field(default_factory=queue.Queue)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> list:
        with self._lock:
            return list(self.messages)

    def wait_for(self, match: Callable[[Any], bool], timeout: float,
                 sleep: Callable[[float], None] = time.sleep, step: float = 0.2) -> Any | None:
        """Первое сообщение, подходящее под ``match``, — уже пришедшее или в пределах ``timeout``."""
        waited = 0.0
        while True:
            for _, message in self.snapshot():
                if match(message):
                    return message
            if waited >= timeout or self.closed.is_set():
                for _, message in self.snapshot():
                    if match(message):
                        return message
                return None
            sleep(step)
            waited += step

    def cancel(self) -> None:
        self._stop.set()
        call = self._call
        if call is not None and hasattr(call, "cancel"):
            try:
                call.cancel()
            except Exception:  # noqa: BLE001 — отмена при закрытии не должна ронять разведку
                pass


class FinamGateway:
    """Сервисы Finam Trade API за ``call("orders.PlaceOrder", request)``."""

    def __init__(self, secret: str, *, account_id: str | None = None, account_placeholder: str = "finam-account",
                 recorder: CallRecorder | None = None, channel_factory: Callable[[str], Any] = _secure_channel,
                 stub_factory: Callable[[Any], dict[str, Any]] = _default_stubs, refresh_jwt: bool = True,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not secret:
            raise ValueError("Не задан секрет Finam Trade API")
        self._secret = secret
        self.recorder = recorder
        self.scrub = recorder.scrub if recorder is not None else Scrubber()
        self.scrub.add(secret, "[secret]")
        register_secrets([secret])
        self.account_id = account_id
        if account_id:
            self.scrub.add(account_id, account_placeholder)
            register_secrets([account_id])
        self._channel_factory = channel_factory
        self._stub_factory = stub_factory
        self._channel = None
        self._stubs: dict[str, Any] = {}
        self.refresh_jwt = refresh_jwt
        self._monotonic = monotonic
        self._sleep = sleep
        self.clock = clock
        self.jwt: str | None = None
        self.jwt_number = 0
        self._jwt_at = 0.0
        self._streams: list[StreamHandle] = []

    # --- канал и JWT ------------------------------------------------------
    def __enter__(self) -> "FinamGateway":
        self._channel = self._channel_factory(TARGET)
        self._stubs = self._stub_factory(self._channel)
        self.authenticate()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        for handle in self._streams:
            handle.cancel()
        channel, self._channel = self._channel, None
        if channel is not None and hasattr(channel, "close"):
            channel.close()
        return False

    def authenticate(self) -> str:
        # JWT становится секретом до записи ответа Auth, иначе он попал бы в responses.jsonl.
        response = self._unary("auth.Auth", auth_service_pb2.AuthRequest(secret=self._secret), auth=False,
                               on_response=lambda reply: self.adopt_jwt(reply.token))
        return response.token

    def adopt_jwt(self, token: str, *, use: bool = True) -> str:
        """Новый JWT: секрет лога и заглушка ``jwt#N`` в записях до любого использования."""
        register_secrets([token])
        self.jwt_number += 1
        label = f"jwt#{self.jwt_number}"
        self.scrub.add(token, label)
        if use:
            self.jwt = token
            self._jwt_at = self._monotonic()
        return label

    def adopt_accounts(self, account_ids) -> None:
        """Номера счетов токена: в записях — ``account-…XXXX``, в логе — ``[скрыто]``."""
        for account_id in account_ids:
            if account_id and account_id != self.account_id:
                register_secrets([account_id])
                self.scrub.add(account_id, f"account-{mask_account(account_id)}")

    def jwt_label(self, token: str) -> str:
        return self.scrub(token) if token else "нет"

    def _metadata(self, jwt: str | None = None, method: str = "") -> tuple:
        if method.split(".", 1)[0] == BODY_AUTH_SERVICE:
            return ()
        token = jwt if jwt is not None else self.jwt
        return (("authorization", token),) if token else ()

    def _stub_method(self, method: str):
        service, name = method.split(".", 1)
        return getattr(self._stubs[service], name)

    # --- вызовы -----------------------------------------------------------
    def describe(self, text: str) -> str:
        """Текст брокера для человека: секреты заменены, номер счёта — последние 4 символа."""
        if self.account_id:
            text = str(text).replace(self.account_id, mask_account(self.account_id))
        return self.scrub(str(text))

    def call(self, method: str, request, *, retry: bool | None = None, jwt: str | None = None,
             summarize: Callable[[Any], Any] | None = None, on_response: Callable[[Any], Any] | None = None):
        """Унарный вызов. ``retry=None`` — повтор лимита только у чтения; ``jwt`` — вызов чужим JWT.

        ``on_response`` вызывается до записи ответа: так новые секреты из ответа заменяются уже в записи.
        """
        if self.refresh_jwt and jwt is None and self._monotonic() - self._jwt_at > JWT_REFRESH_SECS:
            log.info("Финам: JWT старше %d с — новый Auth", JWT_REFRESH_SECS)
            self.authenticate()
        return self._unary(method, request, retry=retry, jwt=jwt, summarize=summarize, on_response=on_response)

    def _unary(self, method, request, *, auth=True, retry=None, jwt=None, summarize=None, on_response=None):
        if retry is None:
            retry = method not in WRITE_METHODS
        attempts = RATE_LIMIT_RETRIES + 1 if retry else 1
        for attempt in range(attempts):
            started = self._monotonic()
            try:
                response = self._stub_method(method)(request, metadata=self._metadata(jwt, method) if auth else (),
                                                     timeout=DEADLINE_SECS)
            except Exception as exc:  # noqa: BLE001 — любой сбой вызова становится BrokerCallError
                error = self._error(method, exc)
                error.metadata_keys = _metadata_keys(exc)
                self._record(method, request, started, error=error, metadata=error.metadata_keys)
                if error.code == "RESOURCE_EXHAUSTED" and attempt + 1 < attempts:
                    delay = _pushback_secs(exc)
                    if delay is None:
                        delay = RATE_LIMIT_BACKOFF_SECS[min(attempt, len(RATE_LIMIT_BACKOFF_SECS) - 1)]
                    delay = min(delay, RATE_LIMIT_MAX_WAIT_SECS)
                    log.warning("Финам: %s — лимит запросов, повтор %d из %d через %g с", method, attempt + 1,
                                attempts - 1, delay)
                    self._sleep(delay)
                    continue
                log.warning("Финам: %s — %s", method, error)
                raise error from None
            if on_response is not None:
                on_response(response)
            self._record(method, request, started, response=summarize(response) if summarize else response)
            log.debug("Финам: %s выполнен", method)
            return response
        raise AssertionError("недостижимо")

    def _error(self, method: str, exc: BaseException) -> BrokerCallError:
        if isinstance(exc, grpc.RpcError) and hasattr(exc, "code"):
            try:
                status = exc.code()
                details = exc.details() if hasattr(exc, "details") else ""
            except Exception:  # noqa: BLE001
                return BrokerCallError(method, type(exc).__name__)
            return BrokerCallError(method, getattr(status, "name", str(status)), self.describe(clean_details(details)))
        return BrokerCallError(method, type(exc).__name__)

    def _record(self, method, request, started, *, response=None, error=None, metadata=None) -> None:
        if self.recorder is None:
            return
        entry: dict[str, Any] = {
            "time": self.clock().isoformat(),
            "method": method,
            "request": request,
            "duration_ms": round((self._monotonic() - started) * 1000),
        }
        if error is not None:
            entry["error"] = {"code": error.code, "details": error.details}
            if metadata:
                entry["error_metadata_keys"] = metadata
        else:
            entry["response"] = response
        self.recorder.write(entry)

    # --- стримы -----------------------------------------------------------
    def open_stream(self, method: str, request, *, jwt: str | None = None, name: str = "",
                    on_message: Callable[[Any], None] | None = None) -> StreamHandle:
        """Стрим в потоке-демоне. ``request`` — сообщение или фабрика итератора запросов (bidi).

        ``on_message`` вызывается до записи сообщения: так новый JWT становится секретом раньше, чем попадёт в записи.
        """
        handle = StreamHandle(method)
        metadata = self._metadata(jwt, method)
        started = self._monotonic()

        def requests() -> Iterator:
            for item in request():
                yield item
            handle._stop.wait()

        def consume() -> None:
            try:
                payload = requests() if callable(request) else request
                call = self._stub_method(method)(payload, metadata=metadata)
                handle._call = call
                for message in call:
                    if on_message is not None:
                        on_message(message)
                    with handle._lock:
                        handle.messages.append((self.clock(), message))
                    self._record(method, None if callable(request) else request, started, response=message)
            except Exception as exc:  # noqa: BLE001 — поток стрима не должен ронять разведку
                if not handle._stop.is_set():
                    handle.error = self._error(method, exc)
                    self._record(method, None if callable(request) else request, started, error=handle.error,
                                 metadata=_metadata_keys(exc))
            finally:
                handle.closed_at = self.clock()
                handle.closed.set()

        thread = threading.Thread(target=consume, name=name or f"finam-{method}", daemon=True)
        self._streams.append(handle)
        thread.start()
        return handle


def iterate(values: Iterable) -> Callable[[], Iterator]:
    """Фабрика запросов bidi-стрима: сначала ``values``, затем ожидание закрытия."""
    items = list(values)
    return lambda: iter(items)
