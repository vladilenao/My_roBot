"""Тесты слоя ошибок: пользователю достаётся причина словами, а не дампом."""

import pytest

from src.events.event import Event
from src.notifier.errors import is_rate_limit
from src.notifier.templates import render


@pytest.mark.parametrize(
    "text",
    [
        "RESOURCE_EXHAUSTED: превышен лимит запросов",
        "rpc error: code = ResourceExhausted desc = quota",
        "resource_exhausted",
    ],
)
def test_rate_limit_is_recognised(text: str) -> None:
    assert is_rate_limit(RuntimeError(text)) is True


@pytest.mark.parametrize(
    "text",
    [
        "connection reset by peer",
        "Не удалось получить свечи",
        "",
    ],
)
def test_ordinary_errors_are_not_rate_limits(text: str) -> None:
    assert is_rate_limit(RuntimeError(text)) is False


def test_error_text_keeps_operation_without_traceback() -> None:
    event = Event.error(operation="анализ NG-10.26 (1h)")

    text = render(event)

    assert text is not None
    assert "анализ NG-10.26 (1h)" in text
    assert "bot_debug.log" in text
    assert "Traceback" not in text
