"""Конечный срок unary RPC брокера, включая запросы свечей и расписания."""

from collections import namedtuple
import grpc


class _Details(namedtuple("Details", "method timeout metadata credentials wait_for_ready compression"), grpc.ClientCallDetails):
    pass


class RpcDeadline(grpc.UnaryUnaryClientInterceptor):
    def __init__(self, seconds=30):
        self.seconds = seconds

    def intercept_unary_unary(self, continuation, details, request):
        timeout = self.seconds if details.timeout is None else min(details.timeout, self.seconds)
        return continuation(_Details(details.method, timeout, details.metadata, details.credentials,
            getattr(details, "wait_for_ready", None), getattr(details, "compression", None)), request)
