"""A tiny in-process, per-key sliding-window rate limiter (Track B/B1). Single
serving process at the pilot scale, so an in-memory limiter is enough; the clock
is injectable for deterministic tests. Not durable and not shared across
processes — a deliberate pilot simplification, replaced by a shared store when
the deployment scales out."""

import threading
import time
from collections import defaultdict
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        max_events: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_events
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record an attempt for `key`; True if it is within the window's
        ceiling, False otherwise. Sweeps expired timestamps (and empty keys) on
        every call so memory stays bounded by what is currently active."""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            # Evict expired keys entirely (bound memory), not just the one hit.
            for k in [k for k, ts in self._events.items()
                      if not ts or ts[-1] <= cutoff]:
                del self._events[k]
            recent = [t for t in self._events[key] if t > cutoff]
            if len(recent) >= self._max:
                self._events[key] = recent
                return False
            recent.append(now)
            self._events[key] = recent
            return True
