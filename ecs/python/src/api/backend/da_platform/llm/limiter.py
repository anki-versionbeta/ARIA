"""Concurrency and rate control for outbound provider calls.

Why this belongs to the platform and not to each silo: BOP issues up to 15 parallel
LLM calls per run, so 15 concurrent runs is roughly 225 simultaneous Iliad requests.
A silo throttling only itself cannot see the other silos' traffic.

**Scope caveat, stated plainly:** this caps concurrency *per process*. Workers are
separate processes (D9), so with M workers the real ceiling is M x the cap. There is
deliberately no Redis (D10), so a cluster-wide cap would need shared state in the
database. Until Iliad's real caps are documented (an open item in spec section 17),
set LLM_MAX_CONCURRENCY to the global budget divided by the worker count.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager


class TokenBucket:
    """`rate` tokens per second, bursting up to `capacity`."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate
        self._capacity = capacity if capacity is not None else rate
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available; returns the seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                delay = (tokens - self._tokens) / self._rate
            # Sleep outside the lock so other threads can still refill and proceed.
            time.sleep(delay)
            waited += delay


class ProviderLimiter:
    """A concurrency ceiling, optionally paired with a sustained rate limit."""

    def __init__(
        self, name: str, max_concurrency: int, rate_per_second: float | None = None
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.name = name
        self.max_concurrency = max_concurrency
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._bucket = TokenBucket(rate_per_second) if rate_per_second else None

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._semaphore.acquire()
        try:
            if self._bucket is not None:
                self._bucket.acquire()
            yield
        finally:
            self._semaphore.release()
