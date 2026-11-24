"""Записи вызовов Finam Trade API (``responses.jsonl``) и замена секретов (add-finam-probe, решение 8).

Секрет, каждый JWT и номер счёта заменяются заглушками до записи строки:
``[secret]``, ``jwt#N``, ``finam-account``. Метаданные запроса не пишутся вовсе.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

from src.sandbox.records import jsonable, mask_account

__all__ = ["CallRecorder", "Scrubber", "mask_account", "to_jsonable"]


def to_jsonable(value: Any) -> Any:
    """Сообщение protobuf → словарь с именами полей proto; остальное — как у песочницы."""
    if isinstance(value, Message):
        return MessageToDict(value, preserving_proto_field_name=True)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return jsonable(value)


class Scrubber:
    """Значения, которые нигде не должны появиться, и их заглушки; длинные — первыми."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.Lock()

    def add(self, value: str | None, placeholder: str) -> None:
        if value:
            with self._lock:
                self._values[str(value)] = placeholder

    def find(self, placeholder: str) -> str | None:
        """Значение по заглушке — например, JWT по ``jwt#N``; наружу не выводится."""
        with self._lock:
            return next((value for value, label in self._values.items() if label == placeholder), None)

    def __call__(self, text: str) -> str:
        with self._lock:
            pairs = sorted(self._values.items(), key=lambda item: len(item[0]), reverse=True)
        for value, placeholder in pairs:
            text = text.replace(value, placeholder)
        return text


class CallRecorder:
    """``responses.jsonl``: строка на вызов или сообщение стрима, секреты заменены."""

    def __init__(self, path: Path, scrubber: Scrubber | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        self.scrub = scrubber or Scrubber()
        self._lock = threading.Lock()

    def write(self, entry: dict) -> None:
        line = self.scrub(json.dumps(to_jsonable(entry), ensure_ascii=False))
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
