"""Unit-тесты отображения ошибок для пользователя."""
import pytest

from src.notifier.errors import user_error_message


class TestUserErrorMessage:
    """Проверка user_error_message()."""

    def test_rate_limit_returns_none(self) -> None:
        exc = RuntimeError(
            "(<StatusCode.RESOURCE_EXHAUSTED: 8>, '', Metadata(ratelimit_remaining=0))"
        )
        assert user_error_message(exc, "обновление данных SBER (15m)") is None

    def test_generic_error_returns_friendly_text_with_operation(self) -> None:
        operation = "анализ NG-9.26 (1h, flat_triangle)"
        message = user_error_message(RuntimeError("connection refused"), operation)
        assert message is not None
        assert operation in message
        assert "bot_debug.log" in message
        assert "connection refused" not in message

    def test_no_rate_limit_marker_returns_text(self) -> None:
        message = user_error_message(ValueError("bad assignment"), "обработка тика")
        assert message is not None
        assert "обработка тика" in message