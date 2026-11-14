from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RequestRateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self._clock = clock
        self._sleep = sleep
        self._minimum_interval = 60.0 / requests_per_minute
        self._lock = threading.Lock()
        self._last_start: float | None = None

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last_start is not None:
                elapsed = now - self._last_start
                remaining = self._minimum_interval - elapsed
                if remaining > 0:
                    self._sleep(remaining)
                    now = self._clock()
            self._last_start = now
