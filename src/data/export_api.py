"""Локальный HTTP-экспорт журнала рыночных данных.

Сервер построен на стандартной библиотеке, чтобы запуск робота не зависел от
web-фреймворка. По умолчанию он слушает только loopback.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.data.market_store import CONTOUR_CONSUMER_PREFIX, CursorExpired, MarketDataStore, ProducerMismatch
from src.logging_setup import get_logger

log = get_logger(__name__)
_CONSUMER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_CONTROL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_CONTOURS = "/api/v1/contours"
# /api/v1/contours/{id}[/stop|/log] — маршруты одного контура (add-control-api).
_CONTOUR = re.compile(r"^/api/v1/contours/([a-z0-9][a-z0-9-]{0,62})(?:/(stop|log|instruments))?$")
_OPENAPI = Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"


class MarketDataExportServer:
    """Управляемый сервер API; ``start`` не блокирует основной цикл робота."""

    def __init__(self, store: MarketDataStore, token: str | None, host: str = "127.0.0.1", port: int = 8101,
                 clusters_report=None, control=None, control_token: str | None = None) -> None:
        if not token and not control_token:
            raise ValueError("Для локального API требуется хотя бы один непустой ключ")
        self.store = store
        # Отчёт кластерного фильтра (src.clusters.report.ClusterReport); None — сервис недоступен.
        self.clusters_report = clusters_report
        # Ключ экспорта и отчёта; None — эти маршруты отвечают 401.
        self.token = token or None
        # Управление контуром (src.bot.launch_control.LaunchControl) и его отдельный ключ.
        self.control = control
        self.control_token = control_token or None
        # Состояние ядра для GET /api/v1/core (add-contour-recovery): функция без аргументов или None.
        self.core_state = None
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

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
        if parsed.path == _CONTOURS:
            if self._control_allowed():
                self._json(HTTPStatus.OK, {"contours": [self.export_server.control.snapshot()]})
            return
        if parsed.path == "/api/v1/core":
            if self._control_allowed():
                if self.export_server.core_state is None:
                    self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Состояние ядра недоступно в этом процессе.")
                else:
                    self._json(HTTPStatus.OK, self.export_server.core_state())
            return
        contour = _CONTOUR.match(parsed.path)
        if contour is not None and contour.group(2) not in ("stop", "instruments"):
            if self._control_allowed():
                self._contour_get(contour.group(1), contour.group(2), parse_qs(parsed.query))
            return
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Токен отсутствует или неверен.")
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
        parsed = urlparse(self.path)
        schedule_match = re.fullmatch(r"/api/v1/market-data/consumers/([^/]+)/trading-schedules-checkpoint", parsed.path)
        match = re.fullmatch(r"/api/v1/market-data/consumers/([^/]+)/checkpoint", parsed.path)
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Токен отсутствует или неверен.")
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
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
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
        from src.bot.launch_control import BadCommand, parse_command

        parsed = urlparse(self.path)
        contour = _CONTOUR.match(parsed.path)
        if contour is not None and contour.group(2) == "stop":
            if self._control_allowed():
                self._contour_stop(contour.group(1))
            return
        if contour is not None and contour.group(2) == "instruments":
            if self._control_allowed():
                self._contour_members(contour.group(1))
            return
        if parsed.path != _CONTOURS:
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
            return
        if not self._control_allowed():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else None
            request = parse_command(body)
        except BadCommand as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.code, exc.message)
            return
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Тело команды — JSON-объект с полями kind и instruments.")
            return
        status, payload = self.export_server.control.submit(request)
        if status == HTTPStatus.ACCEPTED:
            self._json(HTTPStatus(status), payload, headers={"Location": _CONTOURS})
        else:
            self._json(HTTPStatus(status), payload)

    def _contour_get(self, contour_id: str, part: str | None, query: dict) -> None:
        from src.bot.launch_control import LOG_TAIL_DEFAULT, LOG_TAIL_MAX

        control = self.export_server.control
        if part is None:
            snapshot = control.contour(contour_id)
            if snapshot is None:
                self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Контура {contour_id!r} нет.")
            else:
                self._json(HTTPStatus.OK, snapshot)
            return
        raw = query.get("tail", [str(LOG_TAIL_DEFAULT)])
        if len(raw) != 1 or not raw[0].isdigit() or not 1 <= int(raw[0]) <= LOG_TAIL_MAX:
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", f"tail — целое число от 1 до {LOG_TAIL_MAX}.")
            return
        text = control.log_tail(contour_id, int(raw[0]))
        if text is None:
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"Контура {contour_id!r} нет.")
        else:
            self._send_text(HTTPStatus.OK, text, "text/plain; charset=utf-8")

    def _contour_stop(self, contour_id: str) -> None:
        from src.bot.launch_control import BadCommand, parse_stop

        try:
            length = int(self.headers.get("Content-Length", "0"))
            mode = parse_stop(json.loads(self.rfile.read(length).decode("utf-8")) if length else None)
        except BadCommand as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.code, exc.message)
            return
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Тело остановки — JSON-объект с полем mode.")
            return
        status, payload = self.export_server.control.stop(contour_id, mode)
        headers = {"Location": f"{_CONTOURS}/{contour_id}"} if status == HTTPStatus.ACCEPTED else None
        self._json(HTTPStatus(status), payload, headers=headers)

    def _contour_members(self, contour_id: str) -> None:
        from src.bot.launch_control import BadCommand, parse_membership

        try:
            length = int(self.headers.get("Content-Length", "0"))
            command = parse_membership(json.loads(self.rfile.read(length).decode("utf-8")) if length else None)
        except BadCommand as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc.code, exc.message)
            return
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Тело состава — JSON-объект с полем add или remove.")
            return
        status, payload = self.export_server.control.change_membership(contour_id, command)
        headers = {"Location": f"{_CONTOURS}/{contour_id}"} if status == HTTPStatus.ACCEPTED else None
        self._json(HTTPStatus(status), payload, headers=headers)

    def _authorised(self) -> bool:
        return _bearer_matches(self.headers.get("Authorization", ""), self.export_server.token)

    def _control_allowed(self) -> bool:
        """Ключ управления и loopback-``Host``; при отказе ответ уже отправлен."""
        server = self.export_server
        if server.control is None or not _bearer_matches(self.headers.get("Authorization", ""), server.control_token):
            self._error(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Ключ управления отсутствует или неверен.")
            return False
        port = server.address[1]
        if self.headers.get("Host", "") not in {f"{host}:{port}" for host in _CONTROL_HOSTS}:
            self._error(HTTPStatus.FORBIDDEN, "FORBIDDEN_HOST",
                        "Управление доступно только по адресу 127.0.0.1, localhost или [::1].")
            return False
        return True

    def _json(self, status: HTTPStatus, payload: dict, headers: dict | None = None) -> None:
        self._send_text(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        "application/json; charset=utf-8", headers=headers)

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"code": code, "message": message})

    def _send_text(self, status: HTTPStatus, body: str, content_type: str, headers: dict | None = None) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)


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
