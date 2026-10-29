"""Записи вызовов API песочницы для будущих фикстур и маска номера счёта."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

ACCOUNT_PLACEHOLDER = "sandbox-account"


def mask_account(account_id: str) -> str:
    """Номер счёта для человека: только последние четыре символа."""
    return f"…{str(account_id)[-4:]}"


def jsonable(value: Any) -> Any:
    """Ответ SDK (dataclass, enum, Decimal, datetime) в значение для JSON."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if type(value) is object:  # заглушка SDK у незаданного поля сообщения
        return None
    return str(value)


class CallRecorder:
    """``responses.jsonl``: строка на вызов, номер счёта заменён, метаданных нет."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        self.account_id: str | None = None

    def set_account(self, account_id: str) -> None:
        """Номер счёта стал известен: заменить его и в уже записанных строках (ответ GetAccounts)."""
        self.account_id = account_id
        text = self.path.read_text(encoding="utf-8")
        if account_id in text:
            self.path.write_text(text.replace(account_id, ACCOUNT_PLACEHOLDER), encoding="utf-8")

    def write(self, entry: dict) -> None:
        line = json.dumps(jsonable(entry), ensure_ascii=False)
        if self.account_id:
            line = line.replace(self.account_id, ACCOUNT_PLACEHOLDER)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
