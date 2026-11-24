"""Транспорт Finam Trade API контура «Счёт Финам» (add-finam-contour, решения 5–7).

Один TLS-канал на ``api.finam.ru:443`` и заглушки ``src/finam/proto``. Разведку Ф0 (``src.finam.gateway``)
адаптер не импортирует: из неё сюда перешли только проверенные прогонами факты — ``AuthService`` без
заголовка ``authorization`` и описание ошибки Финама в одну строку.

- ``JwtSource`` — единственный владелец JWT: ``Auth`` при входе, затем стрим ``SubscribeJwtRenewal``;
  пока стрима нет и JWT старше 10 минут — новый ``Auth`` перед вызовом. Каждый JWT — секрет лога
  до того, как станет текущим.
- ``RESOURCE_EXHAUSTED``: подсказки повтора Финам не даёт, поэтому пауза — до ``reset_time`` квоты метода
  из ``GetUsageMetrics``; повторов не больше двух. Других повторов по лимиту в адаптере нет.
- Недоступность с 05:00 до 06:15 МСК — плановое окно обслуживания API: ``BrokerOutage``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Any

import grpc

from src.broker_contour.port import BrokerCallError, BrokerOutage, mask_account
from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.auth import auth_service_pb2, auth_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.metrics import usage_metrics_service_pb2, usage_metrics_service_pb2_grpc
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2_grpc
from src.logging_setup import get_logger, register_secrets

log = get_logger(__name__)

TARGET = "api.finam.ru:443"
DEADLINE_SECS = 30
RATE_LIMITED = "RESOURCE_EXHAUSTED"
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_MAX_WAIT_SECS = 65
RATE_LIMIT_FALLBACK_SECS = 60
JWT_MAX_AGE_SECS = 600              # запасной путь: пока стрима нет, JWT старше 10 минут заменяется Auth
JWT_STALE_SECS = 840                # стрим «жив», но JWT не обновлялся 14 минут (живёт 15) — тоже Auth
RENEWAL_BACKOFF_SECS = (5, 10, 30, 60)
MSK = timezone(timedelta(hours=3))
MAINTENANCE = (clock_time(5, 0), clock_time(6, 15))     # ежедневное окно обслуживания API, МСК
OUTAGE_CODES = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED"})
AUTH_SERVICE = "auth"               # AuthService: секрет или JWT — только в теле запроса, без заголовка

STUBS = {
    "auth": auth_service_pb2_grpc.AuthServiceStub,
    "accounts": accounts_service_pb2_grpc.AccountsServiceStub,
    "orders": orders_service_pb2_grpc.OrdersServiceStub,
    "assets": assets_service_pb2_grpc.AssetsServiceStub,
    "metrics": usage_metrics_service_pb2_grpc.UsageMetricsServiceStub,
}


def _secure_channel(target: str):
    return grpc.secure_channel(target, grpc.ssl_channel_credentials())


def _stubs(channel) -> dict[str, Any]:
    return {name: stub(channel) for name, stub in STUBS.items()}


def clean_details(details: str | None) -> str:
    """Описание ошибки Финама в одну строку: без повтора кода gRPC, два языка через « / » (факт Ф0, 23.11)."""
    text = str(details or "")
    for code in ("INVALID_ARGUMENT", "FAILED_PRECONDITION", "NOT_FOUND", "RESOURCE_EXHAUSTED", "ALREADY_EXISTS"):
        text = text.removeprefix(f"{code}: ")
    return " ".join(text.replace("\x0c", " / ").split())


def in_maintenance(moment: datetime) -> bool:
    """Момент попадает в окно обслуживания API Финама 05:00–06:15 МСК."""
    local = moment.astimezone(MSK).time()
    return MAINTENANCE[0] <= local < MAINTENANCE[1]


def _quota_matches(name: str, method: str) -> bool:
    """Квота ``GetUsageMetrics`` вызванного метода: последний сегмент имени совпадает без учёта регистра."""
    last = name.replace("/", ".").rsplit(".", 1)[-1]
    return last.lower() == method.rsplit(".", 1)[-1].lower()


class FinamTransport:
    """Вызовы Finam Trade API: ``call("accounts.GetAccount", request)``; сбой — ``BrokerCallError``."""

    def __init__(self, secret: str, *, account_id: str | None = None,
                 channel_factory: Callable[[str], Any] = _secure_channel,
                 stub_factory: Callable[[Any], dict[str, Any]] = _stubs,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
                 renewal_backoff: tuple[float, ...] = RENEWAL_BACKOFF_SECS):
        if not secret:
            raise ValueError("Не задан секрет Finam Trade API")
        register_secrets([secret])
        if account_id:
            register_secrets([account_id])
        self._secret = secret
        self.account_id = account_id
        self._channel_factory, self._stub_factory = channel_factory, stub_factory
        self._channel = None
        self._stubs: dict[str, Any] = {}
        self.clock, self._sleep = clock, sleep
        self.jwt = JwtSource(self, secret, monotonic=monotonic, backoff=renewal_backoff)

    # --- канал --------------------------------------------------------------
    def __enter__(self) -> "FinamTransport":
        self._channel = self._channel_factory(TARGET)
        self._stubs = self._stub_factory(self._channel)
        try:
            self.jwt.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.jwt.close()
        channel, self._channel = self._channel, None
        if channel is not None and hasattr(channel, "close"):
            channel.close()
        return False

    # --- вызовы -------------------------------------------------------------
    def _metadata(self, method: str) -> tuple:
        if method.split(".", 1)[0] == AUTH_SERVICE:
            return ()
        return (("authorization", self.jwt.token()),)

    def _stub_method(self, method: str):
        service, name = method.split(".", 1)
        return getattr(self._stubs[service], name)

    def call(self, method: str, request):
        """Унарный вызов; лимит — пауза до сброса квоты (до двух повторов); окно — ``BrokerOutage``."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                return self._stub_method(method)(request, metadata=self._metadata(method), timeout=DEADLINE_SECS)
            except BrokerCallError:
                raise                               # ошибка JwtSource уже разобрана
            except Exception as exc:  # noqa: BLE001 — любой сбой вызова становится BrokerCallError
                error = self.error(method, exc)
            if error.code == RATE_LIMITED and attempt < RATE_LIMIT_RETRIES:
                delay = self._reset_wait(method)
                log.warning("Финам: %s — лимит запросов, повтор %d из %d через %.0f с", method, attempt + 1,
                            RATE_LIMIT_RETRIES, delay)
                self._sleep(delay)
                continue
            log.warning("Финам: %s — %s", method, error.code)
            raise error
        raise AssertionError("недостижимо")

    def open_stream(self, method: str, request):
        """Стрим сервера: объект вызова gRPC (итерация и ``cancel``); сбой открытия — ``BrokerCallError``."""
        try:
            return self._stub_method(method)(request, metadata=self._metadata(method))
        except BrokerCallError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self.error(method, exc) from None

    def error(self, method: str, exc: BaseException) -> BrokerCallError:
        """Исключение вызова → ``BrokerCallError`` или ``BrokerOutage``; объект исключения не печатается."""
        code, details = type(exc).__name__, ""
        if isinstance(exc, grpc.RpcError) and hasattr(exc, "code"):
            try:
                status = exc.code()
                code = getattr(status, "name", str(status))
                details = self._describe(clean_details(exc.details() if hasattr(exc, "details") else ""))
            except Exception:  # noqa: BLE001 — разбор ошибки не должен ронять контур
                pass
        outage = in_maintenance(self.clock()) and (code in OUTAGE_CODES or method.split(".", 1)[0] == AUTH_SERVICE)
        return (BrokerOutage if outage else BrokerCallError)(method, code, details)

    def _describe(self, text: str) -> str:
        if self.account_id:
            text = text.replace(self.account_id, mask_account(self.account_id))
        return text.replace(self._secret, "[скрыто]")

    def _reset_wait(self, method: str) -> float:
        """Пауза до ``reset_time`` квоты метода + 1 с, не дольше 65 с; квоты нет — 60 с."""
        try:
            response = self._stub_method("metrics.GetUsageMetrics")(
                usage_metrics_service_pb2.GetUsageMetricsRequest(), metadata=self._metadata("metrics.GetUsageMetrics"),
                timeout=DEADLINE_SECS)
        except Exception as exc:  # noqa: BLE001 — без квоты пауза по умолчанию
            log.warning("Финам: квоты не прочитаны (%s)", type(exc).__name__)
            return RATE_LIMIT_FALLBACK_SECS
        quota = next((item for item in response.quotas if _quota_matches(item.name, method)), None)
        if quota is None or not quota.HasField("reset_time"):
            return RATE_LIMIT_FALLBACK_SECS
        reset = quota.reset_time.ToDatetime(tzinfo=timezone.utc)
        wait = (reset - self.clock()).total_seconds() + 1
        return min(max(wait, 1.0), RATE_LIMIT_MAX_WAIT_SECS)

    def quotas(self) -> dict[str, int]:
        """Квоты методов в минуту: последний сегмент имени → лимит."""
        response = self.call("metrics.GetUsageMetrics", usage_metrics_service_pb2.GetUsageMetricsRequest())
        return {item.name.replace("/", ".").rsplit(".", 1)[-1]: int(item.limit) for item in response.quotas}


class JwtSource:
    """Единственный владелец JWT транспорта: ``Auth``, стрим ``SubscribeJwtRenewal`` и запасной ``Auth``."""

    def __init__(self, transport: FinamTransport, secret: str, *, monotonic: Callable[[], float],
                 backoff: tuple[float, ...]):
        self._transport, self._secret = transport, secret
        self._monotonic, self._backoff = monotonic, backoff
        self._lock = threading.Lock()
        self._jwt: str | None = None
        self._at = 0.0
        self._streaming = threading.Event()        # стрим обновления открыт и живой
        self._closed = threading.Event()
        self._call = None
        self._thread: threading.Thread | None = None
        self.renewals = 0                          # сколько JWT пришло стримом (для тестов и лога)

    def start(self) -> None:
        with self._lock:
            self._auth()
        self._thread = threading.Thread(target=self._run, name="finam-jwt-renewal", daemon=True)
        self._thread.start()

    def token(self) -> str:
        with self._lock:
            age = self._monotonic() - self._at
            if self._jwt is None or age > JWT_STALE_SECS or (not self._streaming.is_set() and age > JWT_MAX_AGE_SECS):
                self._auth()
            return self._jwt

    def close(self) -> None:
        self._closed.set()
        call = self._call
        if call is not None and hasattr(call, "cancel"):
            try:
                call.cancel()
            except Exception:  # noqa: BLE001 — отмена при закрытии не должна ронять контур
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _adopt(self, token: str) -> None:
        register_secrets([token])               # секрет лога — раньше, чем JWT станет текущим
        self._jwt, self._at = token, self._monotonic()

    def _auth(self) -> None:
        response = self._transport.call("auth.Auth", auth_service_pb2.AuthRequest(secret=self._secret))
        self._adopt(response.token)

    def _run(self) -> None:
        attempt = 0
        while not self._closed.is_set():
            try:
                self._call = self._transport.open_stream(
                    "auth.SubscribeJwtRenewal", auth_service_pb2.SubscribeJwtRenewalRequest(secret=self._secret))
                for message in self._call:
                    if not message.token:
                        continue
                    with self._lock:
                        self._adopt(message.token)
                    self._streaming.set()
                    self.renewals += 1
                    attempt = 0
                    log.debug("Финам: JWT обновлён стримом")
            except BrokerCallError as exc:
                log.info("Финам: стрим обновления JWT не открылся (%s)", exc.code)
            except Exception as exc:  # noqa: BLE001 — обрыв стрима: переподписка
                if not self._closed.is_set():
                    log.info("Финам: стрим обновления JWT оборвался (%s)", self._transport.error(
                        "auth.SubscribeJwtRenewal", exc).code)
            self._streaming.clear()
            self._call = None
            if self._closed.wait(self._backoff[min(attempt, len(self._backoff) - 1)]):
                return
            attempt += 1
