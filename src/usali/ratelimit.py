import time
from collections import deque
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
        # A plain dict (not defaultdict): a mere check must NOT create an entry,
        # and a key whose window fully drains is evicted — otherwise an attacker
        # who cycles keys grows the map unboundedly with empty deques.
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        dq = self._events.get(key)
        if dq is not None:
            while dq and now - dq[0] > self._window:
                dq.popleft()
            if not dq:
                del self._events[key]  # evict fully-expired key instead of leaving an empty deque
                dq = None
        if dq is None:
            dq = deque()
            self._events[key] = dq
        if len(dq) >= self._max:
            return False
        dq.append(now)
        return True
