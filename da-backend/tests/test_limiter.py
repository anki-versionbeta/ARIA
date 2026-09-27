from __future__ import annotations

import threading
import time

import pytest

from da_platform.llm.limiter import ProviderLimiter, TokenBucket


def test_limiter_caps_concurrent_holders():
    """The reason this exists: BOP fires 15 parallel calls per run, and 15 concurrent
    runs would otherwise be ~225 simultaneous requests."""
    limiter = ProviderLimiter("test", max_concurrency=3)
    peak = 0
    active = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal peak, active
        with limiter.slot():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak <= 3
    assert active == 0


def test_limiter_releases_its_slot_when_the_body_raises():
    limiter = ProviderLimiter("test", max_concurrency=1)

    with pytest.raises(RuntimeError):
        with limiter.slot():
            raise RuntimeError("boom")

    # A leaked slot would deadlock here.
    with limiter.slot():
        pass


def test_limiter_rejects_a_nonsense_ceiling():
    with pytest.raises(ValueError):
        ProviderLimiter("test", max_concurrency=0)


def test_token_bucket_allows_the_initial_burst_without_waiting():
    bucket = TokenBucket(rate=10, capacity=5)
    for _ in range(5):
        assert bucket.acquire() == 0.0


def test_token_bucket_throttles_once_the_burst_is_spent():
    bucket = TokenBucket(rate=100, capacity=1)
    bucket.acquire()
    waited = bucket.acquire()
    assert waited > 0


def test_token_bucket_rejects_a_nonsense_rate():
    with pytest.raises(ValueError):
        TokenBucket(rate=0)
