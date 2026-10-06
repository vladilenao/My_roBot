"""Однократные запросы Bot API через внедрённый Cloudflare transport."""
from dataclasses import dataclass
from io import BytesIO

import requests

from src.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ApiResult:
    ok: bool
    result: object = None
    uncertain: bool = False


class TelegramTransport:
    def __init__(self, base_url, token, chat_id, timeout=10):
        self._base = base_url.rstrip("/") + "/bot" + token
        self._token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def request(self, method, data=None, photo=None):
        data = {"chat_id": self.chat_id, **(data or {})}
        try:
            if photo is None:
                response = requests.post(self._base + "/" + method, data=data, timeout=self.timeout)
            else:
                with BytesIO(photo) as stream:
                    response = requests.post(self._base + "/" + method, data=data,
                        files={"photo": ("trade.png", stream, "image/png")}, timeout=self.timeout)
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("invalid response")
        except Exception as exc:
            # requests exceptions may embed the entire URL/token; never log them.
            log.warning("Telegram %s: ошибка транспорта (%s), отправка не повторяется.", method, type(exc).__name__)
            return ApiResult(False, uncertain=True)
        description = str(body.get("description", ""))
        if method.startswith("edit") and "message is not modified" in description.lower():
            return ApiResult(True)
        if response.status_code == 200 and body.get("ok") is True:
            result = body.get("result")
            if method in {"sendPhoto", "sendMessage"} and (
                    not isinstance(result, dict) or not isinstance(result.get("message_id"), int)
                    or isinstance(result.get("message_id"), bool) or result["message_id"] <= 0):
                log.warning("Telegram %s: отсутствует подтверждённый message_id.", method)
                return ApiResult(False, uncertain=True)
            return ApiResult(True, result)
        parameters = body.get("parameters") or {}
        log.warning("Telegram %s: HTTP %s, ok=false, retry_after=%s, запрос не повторяется. %s",
                    method, response.status_code, parameters.get("retry_after") if isinstance(parameters, dict) else None,
                    description.replace(self._token, "[скрыто]").replace(self._base, "[адрес скрыт]")[:300])
        return ApiResult(False)
