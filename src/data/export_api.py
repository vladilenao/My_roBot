"""Локальный HTTP-экспорт журнала рыночных данных.

Сервер построен на стандартной библиотеке, чтобы запуск робота не зависел от
web-фреймворка. По умолчанию он слушает только loopback.

С add-history-contour сервер поднимается до ядра: управление работает сразу,
а рыночная база и отчёт кластеров подключаются, когда ядро их открыло
(``attach_market``). До этого маршруты экспорта отвечают ``503``.

С rework-contour-api у каждого контура свой ресурс: ``/api/v1/emulation`` и
``/api/v1/history``; точка входа — ``/api/v1``. Ошибки управления —
``application/problem+json``, экспорта и отчёта кластеров — прежний
``{code, message}``: его читает эмулятор данных.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from src.bot import control_views as views
from src.data.market_store import CONTOUR_CONSUMER_PREFIX, CursorExpired, MarketDataStore, ProducerMismatch
from src.logging_setup import get_logger

log = get_logger(__name__)
_CONSUMER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_CONTROL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
# Прежняя оболочка контуров (до rework-contour-api): все её маршруты отвечают 404.
_CONTOURS = "/api/v1/contours"
# /api/v1/history/{run_id}[/stop|/report|/log]; run_id — имя папки прогона.
_RUN = re.compile(r"^/api/v1/history/([0-9]{8}-[0-9]{6}(?:-[0-9]+)?)(?:/(stop|report|log))?$")
_CONTROL_PREFIXES = (_CONTOURS, views.CORE, views.EMULATION, views.HISTORY, views.SANDBOX)
_OPENAPI = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.yaml"
# Маршруты экспорта рыночных данных и отчёта кластеров: им нужна рыночная база ядра.
_EXPORT_GET = ("/api/v1/market-data/source", "/api/v1/market-data/changes",
               "/api/v1/market-data/trading-schedules", "/api/v1/clusters/report")


class MarketDataExportServer:
    """Управляемый сервер API; ``start`` не блокирует основной цикл робота."""

    def __init__(self, store: MarketDataStore | None, token: str | None, host: str = "127.0.0.1", port: int = 8101,
                 clusters_report=None, control=None, control_token: str | None = None,
                 export_enabled: bool = True) -> None:
        if not token and not control_token:
            raise ValueError("Для локального API требуется хотя бы один непустой ключ")
        # Рыночная база ядра; None — ядро ещё не открыло её (503 MARKET_DATA_NOT_READY).
        self.store = store
        # [market_data] export_enabled = false — маршруты экспорта и отчёта кластеров отвечают 404.
        self.export_enabled = export_enabled
        # Отчёт кластерного фильтра (src.clusters.report.ClusterReport); None — сервис недоступен.
        self.clusters_report = clusters_report
        # Ключ экспорта и отчёта; None — эти маршруты отвечают 401.
        self.token = token or None
        # Управление контуром (src.bot.launch_control.LaunchControl) и его отдельный ключ.
        self.control = control
        self.control_token = control_token or None
        # Состояние ядра для GET /api/v1/core (add-contour-recovery): функция без аргументов или None.
        self.core_state = None
        # Контур «Песочница» (src.bot.sandbox_control.SandboxControl, add-sandbox-contour) или None.
        self.sandbox = None
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def attach_market(self, store: MarketDataStore, clusters_report=None) -> None:
        """Ядро открыло рыночную базу: экспорт и отчёт кластеров начинают отвечать."""
        self.clusters_report = clusters_report
        self.store = store

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            return self.host, self.port
        return self._server.server_address[:2]

    def start(self) -> None:
        if self._server is not None:
            return
        owner = self

        class Handler(_Handler):
            export_server = owner

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="market-data-export", daemon=True)
        self._thread.start()
        host, port = self.address
        log.info("Локальный API робота доступен на http://%s:%s/docs", host, port)

    def close(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


class _Handler(BaseHTTPRequestHandler):
    export_server: MarketDataExportServer
    server_version = "MyRobotMarketData/1.0"
    _body = b""

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - шум access log не нужен
        log.debug("Market data API: " + fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/openapi.yaml":
            self._send_text(HTTPStatus.OK, _OPENAPI.read_text(encoding="utf-8"), "application/yaml; charset=utf-8")
            return
        if parsed.path == "/docs":
            self._send_text(HTTPStatus.OK, _swagger_html(), "text/html; charset=utf-8")
            return
        if parsed.path == views.HOME:
            self._home()
            return
        if _is_control(parsed.path):
            if self._control_allowed():
                self._control_get(parsed.path, parse_qs(parsed.query))
            return
        if not self._export_ready(parsed.path in _EXPORT_GET):
            return
        if parsed.path == "/api/v1/market-data/source":
            self._json(HTTPStatus.OK, self.export_server.store.source_info().as_dict())
            return
        if parsed.path == "/api/v1/market-data/changes":
            query = parse_qs(parsed.query)
            producer_id = _one(query, "producer_id")
            after_id = _integer(query, "after_id")
            limit = _integer(query, "limit", default=500, minimum=1)
            if producer_id is None or after_id is None or limit is None:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "producer_id и after_id обязательны; limit — целое число.")
                return
            try:
                self._json(HTTPStatus.OK, self.export_server.store.changes_after(producer_id, after_id, limit))
            except ProducerMismatch:
                self._error(HTTPStatus.CONFLICT, "PRODUCER_MISMATCH", "Запрошенный producer_id не совпадает с текущим.")
            except CursorExpired:
                self._error(HTTPStatus.GONE, "CURSOR_EXPIRED", "Часть журнала изменений недоступна; выполните сверку.")
            return
        if parsed.path == "/api/v1/market-data/trading-schedules":
            query = parse_qs(parsed.query)
            producer_id = _one(query, "producer_id")
            after_id = _integer(query, "after_id")
            limit = _integer(query, "limit", default=500, minimum=1)
            if producer_id is None or after_id is None or limit is None:
                self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "producer_id и after_id обязательны; limit — целое число.")
                return
            try:
                self._json(HTTPStatus.OK, self.export_server.store.schedule_changes_after(producer_id, after_id, limit))
            except ProducerMismatch:
                self._error(HTTPStatus.CONFLICT, "PRODUCER_MISMATCH", "Запрошенный producer_id не совпадает с текущим.")
            return
        if parsed.path == "/api/v1/clusters/report":
            self._clusters_report(parse_qs(parsed.query))
            return
        self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")

    def _export_ready(self, known: bool) -> bool:
        """Ключ экспорта, включённый экспорт и открытая рыночная база; при отказе ответ уже отправлен."""
        server = self.export_server
        if known and not server.export_enabled:
            self._error(HTTPStatus.NOT_FOUND, "EXPORT_DISABLED", "Экспорт выключен: [market_data] export_enabled = false.")
            return False
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Токен отсутствует или неверен.")
            return False
        if known and server.store is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "MARKET_DATA_NOT_READY",
                        "Ядро ещё не открыло рыночную базу; состояние — GET /api/v1/core.")
            return False
        return True

    # --- точка входа и управление (rework-contour-api) -----------------------------
    def _home(self) -> None:
        """``GET /api/v1``: любой из двух ключей; разделы — по правам ключа."""
        from src import __version__

        server = self.export_server
        header = self.headers.get("Authorization", "")
        control = server.control is not None and _bearer_matches(header, server.control_token)
        export = _bearer_matches(header, server.token)
        if not control and not export:
            self._problem(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Ключ отсутствует или неверен.")
            return
        if control and not self._loopback_host():
            return
        core = server.core_state() if server.core_state is not None else None
        history = server.control.history if control else None
        found = {"self": views.HOME}
        if control:
            found.update(core=views.CORE, emulation=views.EMULATION, history=views.HISTORY)
            if server.sandbox is not None:
                found["sandbox"] = views.SANDBOX
        if export:
            found.update({"market-data": "/api/v1/market-data/source", "clusters-report": "/api/v1/clusters/report"})
        found["docs"] = "/docs"
        self._json(HTTPStatus.OK, {
            "version": __version__,
            "core": {"state": (core or {}).get("state", "ready")},
            "emulation": server.control.summary() if control else None,
            "history": None if not control else (history.limits() if history is not None else
                                                 {"max": 0, "running": 0, "queued": 0}),
            "sandbox": server.sandbox.summary() if control and server.sandbox is not None else None,
            "live": None,
            "_links": {name: {"href": href} for name, href in found.items()},
        })

    def _control_get(self, path: str, query: dict[str, list[str]]) -> None:
        from src.assembly.history_runs import LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX

        server, control = self.export_server, self.export_server.control
        if path == views.CORE:
            if server.core_state is None:
                self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Состояние ядра недоступно в этом процессе.")
            else:
                self._json(HTTPStatus.OK, {**server.core_state(), "_links": views.links(self=views.CORE, home=views.HOME)})
            return
        if path == views.SANDBOX or path.startswith(views.SANDBOX + "/"):
            self._sandbox_get(path, query)
            return
        if path == views.EMULATION:
            view = control.emulation()
            self._json(HTTPStatus.OK, view, headers=views.etag((view["target"] or {}).get("revision")))
            return
        if path == views.EMULATION_TARGET:
            target = control.target()
            if target is None:
                self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эмуляцию ещё не запускали: целевого состояния нет.")
            else:
                self._json(HTTPStatus.OK, target, headers=views.etag(target["revision"]))
            return
        if path == views.EMULATION_INSTRUMENTS:
            members = control.members()
            self._json(HTTPStatus.OK, members, headers=views.etag(members["revision"]))
            return
        if path.startswith(views.EMULATION_INSTRUMENTS + "/") and "/" not in path[len(views.EMULATION_INSTRUMENTS) + 1:]:
            name = unquote(path[len(views.EMULATION_INSTRUMENTS) + 1:])
            member = control.member(name)
            if member is None:
                self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Инструмента {name!r} нет в составе эмуляции.")
            else:
                self._json(HTTPStatus.OK, member)
            return
        if path == views.EMULATION_LOG:
            tail = self._tail(query)
            if tail is not None:
                self._send_text(HTTPStatus.OK, control.log_tail(tail), "text/plain; charset=utf-8")
            return
        history = control.history
        if path == views.HISTORY:
            state = _one(query, "state")
            limit = _integer(query, "limit", default=LIST_LIMIT_DEFAULT, minimum=1)
            if ("state" in query and state not in ("active", "finished")) or limit is None or limit > LIST_LIMIT_MAX:
                self._problem(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST",
                              f"state — active или finished; limit — целое от 1 до {LIST_LIMIT_MAX}.")
                return
            self._json(HTTPStatus.OK, {
                "limits": history.limits() if history is not None else {"max": 0, "running": 0, "queued": 0},
                "items": history.runs(state, limit) if history is not None else [],
                "_links": views.links(self=views.HISTORY, home=views.HOME)})
            return
        run = _RUN.match(path)
        if run is None or run.group(2) == "stop":
            self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
            return
        run_id, part = run.groups()
        if part == "log":
            tail = self._tail(query)
            if tail is None:
                return
            text = None if history is None else history.log_tail(run_id, tail)
            if text is None:
                self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Прогона {run_id!r} нет.")
            else:
                self._send_text(HTTPStatus.OK, text, "text/plain; charset=utf-8")
            return
        if history is None:
            self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Прогона {run_id!r} нет.")
            return
        if part == "report":
            self._reply(*history.report(run_id))
            return
        payload = history.run(run_id)
        if payload is None:
            self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Прогона {run_id!r} нет.")
        else:
            self._json(HTTPStatus.OK, payload)

    def _sandbox_get(self, path: str, query: dict[str, list[str]]) -> None:
        sandbox = self.export_server.sandbox
        if sandbox is None:
            self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Контур «Песочница» недоступен в этом процессе.")
            return
        if path == views.SANDBOX:
            view = sandbox.view()
            self._json(HTTPStatus.OK, view, headers=views.etag((view["target"] or {}).get("revision")))
        elif path == views.SANDBOX_TARGET:
            target = sandbox.target()
            self._json(HTTPStatus.OK, target, headers=views.etag((target or {}).get("revision")))
        elif path == views.SANDBOX_BINDINGS:
            payload = sandbox.bindings()
            self._json(HTTPStatus.OK, payload, headers=views.etag(payload["revision"]))
        elif path == views.SANDBOX_RECONCILE:
            self._json(HTTPStatus.OK, sandbox.reconcile())
        elif path == views.SANDBOX_LOG:
            tail = self._tail(query)
            if tail is not None:
                self._send_text(HTTPStatus.OK, sandbox.log_tail(tail), "text/plain; charset=utf-8")
        else:
            self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")

    def _sandbox_command(self, path: str) -> None:
        from src.bot.launch_control import BadCommand
        from src.bot.sandbox_control import parse_bindings, parse_sandbox_target

        sandbox = self.export_server.sandbox
        if sandbox is None:
            self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Контур «Песочница» недоступен в этом процессе.")
            return
        routes = {("PUT", views.SANDBOX_TARGET), ("POST", views.SANDBOX_BINDINGS), ("POST", views.SANDBOX_REMOVE)}
        if (self.command, path) not in routes:
            self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
            return
        expected = _if_match(self.headers.get("If-Match"))
        try:
            body = self._read_json()
            if path == views.SANDBOX_TARGET:
                mode, bindings = parse_sandbox_target(body)
                status, payload = sandbox.put_target(mode, bindings, expected)
                headers = views.etag((payload.get("target") or {}).get("revision")) if status < 400 else None
                if status == HTTPStatus.ACCEPTED:
                    headers = {**(headers or {}), "Location": views.SANDBOX}
            else:
                remove = path == views.SANDBOX_REMOVE
                ids, mode = parse_bindings(body, remove=remove)
                status, payload = sandbox.change_bindings(ids, remove=remove, mode=mode, expected=expected)
                headers = views.etag(payload.get("revision")) if status < 400 else None
        except BadCommand as exc:
            self._problem(HTTPStatus.BAD_REQUEST, exc.code, exc.message)
            return
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._problem(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Тело команды — JSON-объект.")
            return
        self._reply(status, payload, headers)

    def _tail(self, query: dict[str, list[str]]) -> int | None:
        """``?tail=`` журнала: 1–1000, по умолчанию 200; иначе ответ ``400`` уже отправлен."""
        from src.bot.launch_control import LOG_TAIL_DEFAULT, LOG_TAIL_MAX

        raw = query.get("tail", [str(LOG_TAIL_DEFAULT)])
        if len(raw) != 1 or not raw[0].isdigit() or not 1 <= int(raw[0]) <= LOG_TAIL_MAX:
            self._problem(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", f"tail — целое число от 1 до {LOG_TAIL_MAX}.")
            return None
        return int(raw[0])

    def _control_command(self, path: str) -> None:
        """Команды управления: режим эмуляции, состав пачкой, прогон истории и его остановка."""
        from src.bot.launch_control import BadCommand, parse_add, parse_history, parse_remove, parse_target

        control = self.export_server.control
        if path == views.SANDBOX or path.startswith(views.SANDBOX + "/"):
            self._sandbox_command(path)
            return
        run = _RUN.match(path)
        if run is not None and run.group(2) == "stop" and self.command == "POST":
            # Тело остановки прогона не нужно и не читается.
            history = control.history
            if history is None:
                self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Прогона {run.group(1)!r} нет.")
            else:
                self._reply(*history.stop(run.group(1)))
            return
        routes = {("PUT", views.EMULATION_TARGET), ("POST", views.EMULATION_INSTRUMENTS),
                  ("POST", views.EMULATION_REMOVE), ("POST", views.HISTORY)}
        if (self.command, path) not in routes:
            self._problem(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
            return
        expected = _if_match(self.headers.get("If-Match"))
        try:
            body = self._read_json()
            if path == views.EMULATION_TARGET:
                mode, request = parse_target(body, expected)
                status, payload = control.put_target(mode, request, expected)
                headers = views.etag((payload.get("target") or {}).get("revision")) if status < 400 else None
                if status == HTTPStatus.ACCEPTED:
                    headers = {**(headers or {}), "Location": views.EMULATION}
                self._reply(status, payload, headers)
                return
            if path == views.HISTORY:
                request = parse_history(body)
                if control.history is None:
                    self._problem(HTTPStatus.CONFLICT, "HISTORY_UNAVAILABLE", "Очередь прогонов истории не запущена.")
                    return
                status, payload = control.history.submit(request)
                headers = {"Location": views.run_href(payload["id"])} if status == HTTPStatus.ACCEPTED else None
                self._reply(status, payload, headers)
                return
            command = (parse_add if path == views.EMULATION_INSTRUMENTS else parse_remove)(body, expected)
        except BadCommand as exc:
            self._problem(HTTPStatus.BAD_REQUEST, exc.code, exc.message)
            return
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._problem(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Тело команды — JSON-объект.")
            return
        status, payload = control.change_membership(command)
        self._reply(status, payload, views.etag(payload.get("revision")) if status < 400 else None)

    def _read_json(self):
        return json.loads(self._body.decode("utf-8")) if self._body else None

    def _read_body(self) -> None:
        """Тело читается до любого ответа: недочитанное тело Windows обрывает у клиента (WinError 10053)."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        self._body = self.rfile.read(length) if length > 0 else b""

    def _reply(self, status: int, payload: dict, headers: dict | None = None) -> None:
        """Ответ управления: успех — JSON, ошибка — ``problem+json`` из ``{code, message, …}``."""
        if status >= 400:
            self._problem(HTTPStatus(status), payload.get("code", "INTERNAL"), payload.get("message", ""), payload)
        else:
            self._json(HTTPStatus(status), payload, headers=headers)

    def _clusters_report(self, query: dict[str, list[str]]) -> None:
        from src.clusters.report import ReportError

        report = self.export_server.clusters_report
        if report is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "CLUSTERS_UNAVAILABLE", "Отчёт кластерного фильтра не подключён.")
            return
        try:
            day, period = report.parse(_one(query, "date"), _one(query, "period"))
            payload = report.build(day, period)
        except ReportError as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.code, exc.message)
            return
        except Exception as exc:
            log.warning("Отчёт кластерного фильтра не собран: %s", type(exc).__name__, exc_info=True)
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "STORE_UNAVAILABLE", "База сделок или кластерных событий недоступна.")
            return
        self._send_text(HTTPStatus.OK, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        "application/hal+json; charset=utf-8")

    def do_PUT(self) -> None:  # noqa: N802
        self._read_body()
        parsed = urlparse(self.path)
        if _is_control(parsed.path):
            if self._control_allowed():
                self._control_command(parsed.path)
            return
        schedule_match = re.fullmatch(r"/api/v1/market-data/consumers/([^/]+)/trading-schedules-checkpoint", parsed.path)
        match = re.fullmatch(r"/api/v1/market-data/consumers/([^/]+)/checkpoint", parsed.path)
        if not self._export_ready(bool(match or schedule_match)):
            return
        if not match and not schedule_match:
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
            return
        consumer_id = (schedule_match or match).group(1)
        if not _CONSUMER_ID.fullmatch(consumer_id) or consumer_id.startswith(CONTOUR_CONSUMER_PREFIX):
            # Префикс contour- занят курсорами торговых контуров робота.
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Некорректный consumer_id.")
            return
        try:
            body = json.loads(self._body.decode("utf-8"))
            producer_id, change_id = body["producer_id"], body["change_id"]
            if not isinstance(producer_id, str) or isinstance(change_id, bool) or not isinstance(change_id, int):
                raise ValueError
            method = self.export_server.store.acknowledge_schedule if schedule_match else self.export_server.store.acknowledge
            self._json(HTTPStatus.OK, method(consumer_id, producer_id, change_id))
        except ProducerMismatch:
            self._error(HTTPStatus.CONFLICT, "PRODUCER_MISMATCH", "Запрошенный producer_id не совпадает с текущим.")
        except (ValueError, KeyError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Требуются producer_id и целый change_id.")

    def do_POST(self) -> None:  # noqa: N802
        self._read_body()
        parsed = urlparse(self.path)
        if not _is_control(parsed.path):
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
            return
        if self._control_allowed():
            self._control_command(parsed.path)

    def _authorised(self) -> bool:
        return _bearer_matches(self.headers.get("Authorization", ""), self.export_server.token)

    def _control_allowed(self) -> bool:
        """Ключ управления и loopback-``Host``; при отказе ответ уже отправлен."""
        server = self.export_server
        if server.control is None or not _bearer_matches(self.headers.get("Authorization", ""), server.control_token):
            self._problem(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Ключ управления отсутствует или неверен.")
            return False
        return self._loopback_host()

    def _loopback_host(self) -> bool:
        """``Host`` — loopback-адрес с портом сервера; иначе ответ ``403`` уже отправлен."""
        port = self.export_server.address[1]
        if self.headers.get("Host", "") not in {f"{host}:{port}" for host in _CONTROL_HOSTS}:
            self._problem(HTTPStatus.FORBIDDEN, "FORBIDDEN_HOST",
                          "Управление доступно только по адресу 127.0.0.1, localhost или [::1].")
            return False
        return True

    def _json(self, status: HTTPStatus, payload: dict, headers: dict | None = None) -> None:
        self._send_text(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        "application/json; charset=utf-8", headers=headers)

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        """Ошибка экспорта и отчёта кластеров — формат 1.8 ``{code, message}``."""
        self._json(status, {"code": code, "message": message})

    def _problem(self, status: HTTPStatus, code: str, detail: str, extra: dict | None = None) -> None:
        """Ошибка управления — ``application/problem+json`` (RFC 9457)."""
        self._send_text(status, json.dumps(views.problem(status, code, detail, extra), ensure_ascii=False,
                                           separators=(",", ":")), "application/problem+json; charset=utf-8")

    def _send_text(self, status: HTTPStatus, body: str, content_type: str, headers: dict | None = None) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)


def _is_control(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _CONTROL_PREFIXES)


def _if_match(header: str | None) -> int | None:
    """``If-Match: "3"`` → 3; нет заголовка или ``*`` — без проверки; неразборчивое значение не совпадёт ни с чем."""
    if header is None or header.strip() == "*":
        return None
    value = header.strip().removeprefix("W/").strip('"')
    return int(value) if value.isdigit() else -1


def _bearer_matches(header: str, token: str | None) -> bool:
    prefix = "Bearer "
    return bool(token) and header.startswith(prefix) and hmac.compare_digest(header[len(prefix):], token)


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values and len(values) == 1 else None


def _integer(query: dict[str, list[str]], key: str, default: int | None = None, minimum: int = 0) -> int | None:
    value = _one(query, key)
    if value is None:
        return default
    try:
        result = int(value)
    except ValueError:
        return None
    return result if result >= minimum else None


def _swagger_html() -> str:
    return """<!doctype html><html><head><meta charset=\"utf-8\"><title>My Robot API</title>
<link rel=\"stylesheet\" href=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui.css\"></head><body><div id=\"swagger-ui\"></div>
<script src=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js\"></script><script>SwaggerUIBundle({url:'/openapi.yaml',dom_id:'#swagger-ui',persistAuthorization:true});</script></body></html>"""
