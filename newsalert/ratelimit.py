"""Async sliding-window rate limiter with several windows (e.g. per second and per minute)."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable


class RateLimiter:
    def __init__(
        self,
        windows: list[tuple[int, float]],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        """windows: list of (max_calls, period_seconds)."""
        self._windows = [(n, p, deque()) for n, p in windows]
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._paused_until = 0.0

    def pause(self, seconds: float) -> None:
        """Block all callers for `seconds` (used after an HTTP 429)."""
        self._paused_until = max(self._paused_until, self._clock() + seconds)

    def _wait_time(self, now: float) -> float:
        wait = max(0.0, self._paused_until - now)
        for n, period, calls in self._windows:
            while calls and calls[0] <= now - period:
                calls.popleft()
            if len(calls) >= n:
                wait = max(wait, calls[0] + period - now)
        return wait

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                wait = self._wait_time(now)
                if wait <= 0:
                    for _, _, calls in self._windows:
                        calls.append(now)
                    return
                await self._sleep(wait)
