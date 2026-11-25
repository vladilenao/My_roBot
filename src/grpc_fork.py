"""Настройки gRPC до его загрузки: ``run.py`` импортирует модуль первым.

**Без обработчиков fork()** (add-contour-process). Ядро многопоточно и держит вызовы gRPC брокера, а
``subprocess`` на macOS запускает процесс контура через fork() и exec. Поддержка fork в gRPC при
этом печатает в консоль ядра сотни строк ``fork_posix.cc`` / ``ev_poll_posix.cc`` и ничего не даёт:
дочерний процесс сразу выполняет exec и gRPC родителя не использует.

**Системный DNS-резолвер процесса контура** (add-finam-contour). Если описание контура из
``run.py --contour <номер>`` просит ``native_dns``, задаётся ``GRPC_DNS_RESOLVER=native``: gRPC читает
его один раз при загрузке, позже он уже не действует. Остальные процессы — с резолвером по умолчанию.

Значения, заданные снаружи, не меняются.
"""

import os
import sys

os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")


def dns_resolver_for(argv: list[str]) -> str | None:
    """Резолвер gRPC процесса по аргументам ``run.py``: ``native`` или ``None`` — по умолчанию."""
    if argv[:1] != ["--contour"] or len(argv) < 2:
        return None
    from src.broker_contour.contours import live_contour   # описание — данные, без gRPC

    spec = live_contour(argv[1])
    return "native" if spec is not None and spec.native_dns else None


_resolver = dns_resolver_for(sys.argv[1:])
if _resolver is not None:
    os.environ.setdefault("GRPC_DNS_RESOLVER", _resolver)
