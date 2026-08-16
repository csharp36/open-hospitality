import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    """Per-key sliding-window limiter, in-process (fine for a single Cloud Run
    instance at pilot scale — see the Track A design §14)."""

    def __init__(
        self, *, max_events: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_events
        self._window = window_s
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        dq = self._events[key]
        while dq and now - dq[0] > self._window:
            dq.popleft()
        if len(dq) >= self._max:
            return False
        dq.append(now)
        return True
