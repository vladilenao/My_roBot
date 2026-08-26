from unittest.mock import patch, MagicMock, call
import pytest
from src.api.retry import (
    api_call_with_retry, with_retry,
    _is_rate_limited, _parse_reset_delay,
    DEFAULT_MAX_RETRIES, DEFAULT_BASE_DELAY,
)


class TestIsRateLimited:
    def test_resource_exhausted_in_message(self):
        exc = Exception("RESOURCE_EXHAUSTED")
        assert _is_rate_limited(exc) is True

    def test_resource_exhausted_lowercase(self):
        exc = Exception("resource_exhausted")
        assert _is_rate_limited(exc) is True

    def test_other_error(self):
        exc = Exception("connection refused")
        assert _is_rate_limited(exc) is False

    def test_grpc_status_code(self):
        exc = Exception("StatusCode.RESOURCE_EXHAUSTED: resource exhausted")
        assert _is_rate_limited(exc) is True


class TestParseResetDelay:
    def test_parses_ratelimit_reset(self):
        exc = Exception("ratelimit_reset=10, message=None")
        assert _parse_reset_delay(exc) == 10

    def test_parses_large_reset(self):
        exc = Exception("ratelimit_reset=120")
        assert _parse_reset_delay(exc) == 120

    def test_no_reset_returns_default(self):
        exc = Exception("some error")
        assert _parse_reset_delay(exc) == DEFAULT_BASE_DELAY

    def test_zero_reset_returns_one(self):
        exc = Exception("ratelimit_reset=0")
        assert _parse_reset_delay(exc) == 1


class TestApiCallWithRetry:
    @patch("src.api.retry.time.sleep")
    def test_success_first_try(self, mock_sleep):
        fn = MagicMock(return_value="ok")
        result = api_call_with_retry(fn, "arg1", key="val")
        assert result == "ok"
        fn.assert_called_once_with("arg1", key="val")
        mock_sleep.assert_not_called()

    @patch("src.api.retry.time.sleep")
    def test_retry_on_rate_limit(self, mock_sleep):
        fn = MagicMock(side_effect=[
            Exception("RESOURCE_EXHAUSTED"),
            "ok",
        ])
        result = api_call_with_retry(fn, max_retries=3, base_delay=5)
        assert result == "ok"
        assert fn.call_count == 2
        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args[0][0]
        assert 5 <= slept <= 10

    @patch("src.api.retry.time.sleep")
    def test_exhausted_retries_raises(self, mock_sleep):
        fn = MagicMock(side_effect=Exception("RESOURCE_EXHAUSTED"))
        with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
            api_call_with_retry(fn, max_retries=2, base_delay=1)
        assert fn.call_count == 3

    @patch("src.api.retry.time.sleep")
    def test_non_rate_limit_error_raises_immediately(self, mock_sleep):
        fn = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError, match="bad input"):
            api_call_with_retry(fn)
        fn.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("src.api.retry.time.sleep")
    def test_exponential_backoff(self, mock_sleep):
        fn = MagicMock(side_effect=[
            Exception("RESOURCE_EXHAUSTED"),
            Exception("RESOURCE_EXHAUSTED"),
            "ok",
        ])
        result = api_call_with_retry(fn, max_retries=3, base_delay=5, max_delay=60)
        assert result == "ok"
        assert fn.call_count == 3
        assert mock_sleep.call_count == 2
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        assert delays[0] < delays[1]

    @patch("src.api.retry.time.sleep")
    def test_delay_capped_by_ratelimit_reset(self, mock_sleep):
        fn = MagicMock(side_effect=[
            Exception("RESOURCE_EXHAUSTED ratelimit_reset=2"),
            "ok",
        ])
        result = api_call_with_retry(fn, max_retries=3, base_delay=10)
        assert result == "ok"
        slept = mock_sleep.call_args[0][0]
        assert slept <= 2

    @patch("src.api.retry.time.sleep")
    def test_delay_capped_by_max_delay(self, mock_sleep):
        fn = MagicMock(side_effect=[
            Exception("RESOURCE_EXHAUSTED ratelimit_reset=200"),
            "ok",
        ])
        result = api_call_with_retry(fn, max_retries=3, base_delay=10, max_delay=30)
        assert result == "ok"
        slept = mock_sleep.call_args[0][0]
        assert slept <= 30

    @patch("src.api.retry.time.sleep")
    def test_kwargs_forwarded(self, mock_sleep):
        fn = MagicMock(return_value=42)
        result = api_call_with_retry(fn, a=1, b=2)
        assert result == 42
        fn.assert_called_once_with(a=1, b=2)


class TestWithRetryDecorator:
    @patch("src.api.retry.time.sleep")
    def test_decorator_without_args(self, mock_sleep):
        @with_retry
        def my_fn(x):
            return x * 2

        assert my_fn(5) == 10

    @patch("src.api.retry.time.sleep")
    def test_decorator_with_args(self, mock_sleep):
        @with_retry(max_retries=2, base_delay=1)
        def my_fn(x):
            return x * 2

        assert my_fn(5) == 10

    @patch("src.api.retry.time.sleep")
    def test_decorator_retries(self, mock_sleep):
        call_count = [0]

        @with_retry(max_retries=2, base_delay=1)
        def my_fn():
            call_count[0] += 1
            if call_count[0] < 3:
                raise Exception("RESOURCE_EXHAUSTED")
            return "done"

        result = my_fn()
        assert result == "done"
        assert call_count[0] == 3

    @patch("src.api.retry.time.sleep")
    def test_decorator_preserves_name(self, mock_sleep):
        @with_retry
        def my_special_function():
            pass

        assert my_special_function.__name__ == "my_special_function"


class TestDefaults:
    def test_max_retries(self):
        assert DEFAULT_MAX_RETRIES == 3

    def test_base_delay(self):
        assert DEFAULT_BASE_DELAY == 10
