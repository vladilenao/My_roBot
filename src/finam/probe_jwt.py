"""``probe-jwt`` и ``probe-limits``: жизнь JWT и стримов, ответ при превышении лимита (add-finam-probe, решение 5).

Оба действия только читают. ``probe-jwt`` работает с выключенным обновлением
JWT: один ``Auth``, стрим заявок со старым JWT, ``SubscribeJwtRenewal`` по
секрету и раз в минуту ``GetAccount`` со старым JWT. ``probe-limits`` вызывает
``Clock`` подряд без повторов до первого отказа.
"""

from __future__ import annotations

from datetime import datetime

from src.finam.account import usage_metrics
from src.finam.checks import ERROR, NO, SKIPPED, YES, Run, ts
from src.finam.proto.grpc.tradeapi.v1.accounts import accounts_service_pb2 as accounts_pb2
from src.finam.proto.grpc.tradeapi.v1.assets import assets_service_pb2 as assets_pb2
from src.finam.proto.grpc.tradeapi.v1.auth import auth_service_pb2 as auth_pb2
from src.finam.proto.grpc.tradeapi.v1.orders import orders_service_pb2 as orders_pb2
from src.sandbox.gateway import BrokerCallError


class JwtProbe(Run):
    TITLE = "Разведка Финама: JWT и стримы"
    CHECKS = (
        ("Ф0.40", "Срок жизни JWT"),
        ("Ф0.41", "SubscribeJwtRenewal"),
        ("Ф0.42", "Вызов со старым JWT"),
        ("Ф0.43", "Стрим на старом JWT"),
    )

    def __init__(self, *args, minutes: int = 20, secret: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.minutes = minutes
        self._secret = secret
        self.renewals: list[tuple[datetime, str]] = []

    def _minute(self, moment: datetime) -> str:
        return f"{(moment - self.started_at).total_seconds() / 60:.1f} мин"

    def _run(self) -> None:
        gateway = self.gateway
        old = gateway.jwt
        old_label = gateway.jwt_label(old)
        token = self.token
        if token.expires_at and token.created_at:
            life = (token.expires_at - token.created_at).total_seconds() / 60
            self._set("Ф0.40", YES, f"{old_label}: создан {token.created_at:%H:%M:%S}, истекает "
                                    f"{token.expires_at:%H:%M:%S} UTC — {life:g} мин")
        else:
            self._set("Ф0.40", NO, "TokenDetails не назвал срок")
        orders = gateway.open_stream("orders.SubscribeOrders",
                                     orders_pb2.SubscribeOrdersRequest(account_id=self.account_id), jwt=old)

        def renewed(message) -> None:
            label = gateway.adopt_jwt(message.token, use=False)
            self.renewals.append((self.timing.clock(), label))

        renewal = gateway.open_stream("auth.SubscribeJwtRenewal",
                                      auth_pb2.SubscribeJwtRenewalRequest(secret=self._secret), on_message=renewed)
        failure = None
        checks = []
        for minute in range(1, self.minutes + 1):
            self.timing.sleep(self.timing.minute_secs)
            try:
                gateway.call("accounts.GetAccount", accounts_pb2.GetAccountRequest(account_id=self.account_id),
                             jwt=old)
                checks.append(f"{minute}: ок")
            except BrokerCallError as exc:
                checks.append(f"{minute}: {exc.code}")
                if failure is None:
                    failure = (minute, exc)
        self._renewal_result(renewal, gateway)
        if failure is None:
            self._set("Ф0.42", NO, f"{old_label} работал все {self.minutes} мин — отказа не было")
        else:
            minute, exc = failure
            self._set("Ф0.42", YES, f"{old_label}: первый отказ на {minute}-й минуте: {exc}; по минутам "
                                    + ", ".join(checks))
        self._stream_result(orders, old_label)
        orders.cancel()
        renewal.cancel()

    def _renewal_result(self, renewal, gateway) -> None:
        if not self.renewals:
            state = f"ошибка {renewal.error}" if renewal.error else ("закрыт" if renewal.closed.is_set() else "открыт")
            self._set("Ф0.41", NO, f"новых JWT за {self.minutes} мин не пришло; стрим {state}")
            return
        times = ", ".join(f"{label} на {self._minute(moment)}" for moment, label in self.renewals)
        detail = f"новых JWT {len(self.renewals)}: {times}"
        last_label = self.renewals[-1][1]
        token = gateway.scrub.find(last_label)
        if token:
            try:
                info = gateway.call("auth.TokenDetails", auth_pb2.TokenDetailsRequest(token=token),
                                    on_response=lambda reply: gateway.adopt_accounts(reply.account_ids))
                detail += f"; {last_label} создан {ts(info.created_at)}, истекает {ts(info.expires_at)}"
            except BrokerCallError as exc:
                detail += f"; TokenDetails {last_label}: {exc}"
        self._set("Ф0.41", YES, detail)

    def _stream_result(self, handle, label: str) -> None:
        messages = len(handle.snapshot())
        if handle.error is not None:
            self._set("Ф0.43", YES, f"стрим на {label} закрыт на {self._minute(handle.closed_at)}: {handle.error}; "
                                    f"сообщений до закрытия {messages}")
        elif handle.closed.is_set():
            self._set("Ф0.43", YES, f"стрим на {label} закрыт сервером без ошибки на "
                                    f"{self._minute(handle.closed_at)}; сообщений {messages}")
        else:
            self._set("Ф0.43", NO, f"стрим на {label} открыт все {self.minutes} мин; сообщений {messages}")


class LimitsProbe(Run):
    TITLE = "Разведка Финама: лимиты запросов"
    CHECKS = (
        ("Ф0.50", "Квоты до"),
        ("Ф0.51", "Отказ по лимиту"),
        ("Ф0.52", "Квоты после"),
    )

    def __init__(self, *args, calls: int = 250, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = calls

    def _run(self) -> None:
        self._step("Ф0.50", lambda: self._set("Ф0.50", YES, usage_metrics(self.gateway)) or True)
        self._step("Ф0.51", self._flood)
        self._step("Ф0.52", lambda: self._set("Ф0.52", YES, usage_metrics(self.gateway)) or True)

    def _flood(self) -> bool:
        started = self.timing.clock()
        for number in range(1, self.calls + 1):
            try:
                self.gateway.call("assets.Clock", assets_pb2.ClockRequest(), retry=False)
            except BrokerCallError as exc:
                seconds = (self.timing.clock() - started).total_seconds()
                keys = ", ".join(getattr(exc, "metadata_keys", []) or []) or "нет"
                outcome = YES if exc.code == "RESOURCE_EXHAUSTED" else ERROR
                self._set("Ф0.51", outcome, f"отказ на {number}-м вызове Clock за {seconds:.1f} с: {exc}; "
                                            f"ключи метаданных ответа: {keys}")
                return outcome == YES
        seconds = (self.timing.clock() - started).total_seconds()
        self._set("Ф0.51", SKIPPED, f"{self.calls} вызовов Clock за {seconds:.1f} с — отказа не было")
        return True


__all__ = ["JwtProbe", "LimitsProbe"]
