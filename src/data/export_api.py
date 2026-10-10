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

from src.data.market_store import CursorExpired, MarketDataStore, ProducerMismatch
from src.logging_setup import get_logger

log = get_logger(__name__)
_CONSUMER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_OPENAPI = Path(__file__).resolve().parents[2] / "docs" / "market-data" / "openapi.yaml"


class MarketDataExportServer:
    """Управляемый сервер API; ``start`` не блокирует основной цикл робота."""

    def __init__(self, store: MarketDataStore, token: str, host: str = "127.0.0.1", port: int = 8101) -> None:
        if not token:
            raise ValueError("Для экспортного API требуется непустой токен")
        self.store = store
        self.token = token
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
        log.info("Экспорт рыночных данных доступен на http://%s:%s/docs", host, port)

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
        self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/v1/market-data/consumers/([^/]+)/checkpoint", parsed.path)
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Токен отсутствует или неверен.")
            return
        if not match:
            self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Эндпоинт не найден.")
            return
        consumer_id = match.group(1)
        if not _CONSUMER_ID.fullmatch(consumer_id):
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Некорректный consumer_id.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            producer_id, change_id = body["producer_id"], body["change_id"]
            if not isinstance(producer_id, str) or isinstance(change_id, bool) or not isinstance(change_id, int):
                raise ValueError
            self._json(HTTPStatus.OK, self.export_server.store.acknowledge(consumer_id, producer_id, change_id))
        except ProducerMismatch:
            self._error(HTTPStatus.CONFLICT, "PRODUCER_MISMATCH", "Запрошенный producer_id не совпадает с текущим.")
        except (ValueError, KeyError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "Требуются producer_id и целый change_id.")

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return header.startswith(prefix) and hmac.compare_digest(header[len(prefix):], self.export_server.token)

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        self._send_text(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"code": code, "message": message})

    def _send_text(self, status: HTTPStatus, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


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
    return """<!doctype html><html><head><meta charset=\"utf-8\"><title>My Robot Market Data API</title>
<link rel=\"stylesheet\" href=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui.css\"></head><body><div id=\"swagger-ui\"></div>
<script src=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js\"></script><script>SwaggerUIBundle({url:'/openapi.yaml',dom_id:'#swagger-ui',persistAuthorization:true});</script></body></html>"""
