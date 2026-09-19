import time
import functools

from src.logging_setup import get_logger

log = get_logger(__name__)


DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 10
DEFAULT_MAX_DELAY = 60


def _is_rate_limited(exc):
    return "RESOURCE_EXHAUSTED" in str(exc) or "resource_exhausted" in str(exc).lower()


def rate_limit_reset_secs(exc) -> float | None:
    """Секунды до сброса лимита API из исключения rate-limit (публичный хелпер).

    Читает ``ratelimit_reset=N`` (или ``ratelimit_reset = N``) из сообщения ошибки
    и возвращает число секунд (>=1). Если подсказки в тексте нет — ``None``, и
    вызывающий опирается на собственную паузу/троттлинг, а не на сброс лимита.
    """
    text = str(exc)
    idx = text.find("ratelimit_reset")
    if idx == -1:
        return None
    rest = text[idx + len("ratelimit_reset"):]
    if not rest.startswith("="):
        return None
    digit_part = ""
    for ch in rest[1:]:
        if ch.isdigit():
            digit_part += ch
        else:
            break
    reset = int(digit_part)
    # reset=0 в тексте ошибки не бывает осмысленным, но тест фиксирует
    # контракт: даже нулевая подсказка даёт минимальную паузу в 1 секунду.
    return max(reset, 1)


def _parse_reset_delay(exc):
    """Пауза до сброса лимита; без подсказки — базовый интервал ретрая."""
    reset = rate_limit_reset_secs(exc)
    return reset if reset is not None else DEFAULT_BASE_DELAY


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
            log.warning("Rate limit (попытка %d/%d). Ожидание %dс...", attempt + 1, max_retries, delay)
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
