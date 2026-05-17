"""Tests for agent.kalshi_rate_limiter (Session 146)."""

import threading
import time

import pytest

from agent import kalshi_rate_limiter as rl


@pytest.fixture(autouse=True)
def _reset_limiter():
    rl._reset_for_tests()
    yield
    rl._reset_for_tests()


def _fast_limiter(max_concurrent=2, max_tokens=10, window=1.0):
    return rl.KalshiRateLimiter(max_concurrent, max_tokens, window)


def test_sequential_acquires_within_budget_do_not_block():
    lim = _fast_limiter(max_concurrent=2, max_tokens=10, window=1.0)
    start = time.monotonic()
    for _ in range(5):
        lim.acquire()
        lim.release()
    assert time.monotonic() - start < 0.5


def test_concurrent_acquires_block_at_semaphore_cap():
    lim = _fast_limiter(max_concurrent=2, max_tokens=100, window=1.0)
    held: list[int] = []
    release_evt = threading.Event()

    def worker():
        lim.acquire()
        held.append(1)
        release_evt.wait(timeout=2.0)
        lim.release()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    assert len(held) == 2  # only 2 in flight
    release_evt.set()
    for t in threads:
        t.join(timeout=2.0)
    assert len(held) == 4


def test_token_bucket_blocks_when_exhausted():
    lim = _fast_limiter(max_concurrent=10, max_tokens=3, window=1.0)
    start = time.monotonic()
    for _ in range(3):
        lim.acquire()
        lim.release()
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.1
    lim.acquire()  # 4th must wait for refill (window/max_tokens ~ 0.33s)
    lim.release()
    assert time.monotonic() - start > 0.25


def test_refill_rate_matches_window():
    lim = _fast_limiter(max_concurrent=10, max_tokens=4, window=1.0)
    for _ in range(4):
        lim.acquire()
        lim.release()
    time.sleep(0.6)  # 0.6s should refill ~2.4 tokens (slack for scheduler jitter)
    start = time.monotonic()
    lim.acquire()
    lim.release()
    lim.acquire()
    lim.release()
    assert time.monotonic() - start < 0.2  # both should be immediate


def test_acquire_timeout_raises_and_does_not_leak_token():
    lim = _fast_limiter(max_concurrent=1, max_tokens=10, window=1.0)
    lim.acquire()
    with pytest.raises(TimeoutError):
        lim.acquire(timeout=0.1)
    lim.release()
    lim.acquire(timeout=0.5)  # now available
    lim.release()


def test_with_rate_limit_decorator_invokes_and_releases():
    calls: list[int] = []

    @rl.with_rate_limit
    def wrapped(x):
        calls.append(x)
        return x * 2

    assert wrapped(5) == 10
    assert calls == [5]
    # Slot must be released -- limiter should still serve future calls.
    for i in range(3):
        assert wrapped(i) == i * 2


def test_with_rate_limit_releases_on_exception():
    @rl.with_rate_limit
    def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        boom()

    @rl.with_rate_limit
    def ok():
        return 1

    # If release didn't fire on the exception path, this would deadlock at the
    # global limiter's semaphore. The fact that it returns is the assertion.
    assert ok() == 1
