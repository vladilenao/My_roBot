import time
import functools


DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 10
DEFAULT_MAX_DELAY = 60


def _is_rate_limited(exc):
    return "RESOURCE_EXHAUSTED" in str(exc) or "resource_exhausted" in str(exc).lower()


def _parse_reset_delay(exc):
    text = str(exc)
    idx = text.find("ratelimit_reset=")
    if idx == -1:
        return DEFAULT_BASE_DELAY
    rest = text[idx + len("ratelimit_reset="):]
    num = ""
    for ch in rest:
        if ch.isdigit():
            num += ch
        else:
            break
    if num:
        return max(int(num), 1)
    return DEFAULT_BASE_DELAY


def api_call_with_retry(fn, *args, max_retries=DEFAULT_MAX_RETRIES, base_delay=DEFAULT_BASE_DELAY, max_delay=DEFAULT_MAX_DELAY, **kwargs):
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_rate_limited(exc) or attempt == max_retries:
                raise
            last_exc = exc
            reset = _parse_reset_delay(exc)
            delay = min(base_delay * (2 ** attempt), reset, max_delay)
            print(f"  Rate limit (попытка {attempt + 1}/{max_retries}). Ожидание {delay}с...")
            time.sleep(delay)
    raise last_exc


def with_retry(fn=None, *, max_retries=DEFAULT_MAX_RETRIES, base_delay=DEFAULT_BASE_DELAY, max_delay=DEFAULT_MAX_DELAY):
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            return api_call_with_retry(f, *args, max_retries=max_retries, base_delay=base_delay, max_delay=max_delay, **kwargs)
        return wrapper

    if fn is not None:
        return decorator(fn)
    return decorator
