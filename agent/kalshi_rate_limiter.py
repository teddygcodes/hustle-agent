"""
Kalshi rate limiter (Session 146).

Caps concurrent in-flight Kalshi REST calls AND sustained call rate, to
prevent 429 retry storms that amplify into the EDEADLK pattern observed
2026-05-17 (forensics: bot/state/forensics/2026-05-17-edeadlk/).

Pairs with Session 143's run_in_executor wrap (Battle Scar #13): S143
moved sync HTTP off the event loop into worker threads; S146 caps how
many worker threads can be in-flight simultaneously and how fast they
fire sustained. Battle Scar #18.

Default values are deliberately conservative -- Kalshi does not publish
per-key limits and bot was already in a fragile 429-storm state at ship
time. Loosen via watch-list once 7d of zero-429 evidence accumulates.
"""

import threading
import time

KALSHI_MAX_CONCURRENT = 2
KALSHI_RATE_TOKENS = 20
KALSHI_RATE_WINDOW_SEC = 60


class KalshiRateLimiter:
    def __init__(self, max_concurrent: int, max_tokens: int, window_sec: float):
        self._sem = threading.Semaphore(max_concurrent)
        self._max_tokens = max_tokens
        self._window = window_sec
        self._tokens = float(max_tokens)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                float(self._max_tokens),
                self._tokens + elapsed * (self._max_tokens / self._window),
            )
            self._last_refill = now

    def acquire(self, timeout: float | None = None) -> None:
        if not self._sem.acquire(timeout=timeout):
            raise TimeoutError("KalshiRateLimiter: concurrent slot timeout")
        try:
            while True:
                with self._lock:
                    self._refill_locked()
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    deficit = 1.0 - self._tokens
                    wait_s = max(0.05, deficit * (self._window / self._max_tokens))
                time.sleep(wait_s)
        except BaseException:
            self._sem.release()
            raise

    def release(self) -> None:
        self._sem.release()


_LIMITER = KalshiRateLimiter(
    KALSHI_MAX_CONCURRENT, KALSHI_RATE_TOKENS, KALSHI_RATE_WINDOW_SEC
)


def with_rate_limit(fn):
    """Decorator: acquire the global limiter, call fn, release on exit."""
    def wrapper(*args, **kwargs):
        _LIMITER.acquire()
        try:
            return fn(*args, **kwargs)
        finally:
            _LIMITER.release()
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    wrapper.__wrapped__ = fn
    return wrapper


def _reset_for_tests() -> None:
    """Test-only -- re-initialize the global limiter to defaults."""
    global _LIMITER
    _LIMITER = KalshiRateLimiter(
        KALSHI_MAX_CONCURRENT, KALSHI_RATE_TOKENS, KALSHI_RATE_WINDOW_SEC
    )
