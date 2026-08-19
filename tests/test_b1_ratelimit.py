"""In-process sliding-window rate limiter with an injectable clock."""

from usali.ratelimit import RateLimiter


def test_allows_up_to_the_limit_then_blocks():
    now = [1000.0]
    rl = RateLimiter(max_events=3, window_seconds=60, clock=lambda: now[0])
    assert [rl.allow("k") for _ in range(3)] == [True, True, True]
    assert rl.allow("k") is False


def test_window_slides_so_old_events_expire():
    now = [1000.0]
    rl = RateLimiter(max_events=2, window_seconds=60, clock=lambda: now[0])
    assert rl.allow("k") and rl.allow("k")
    assert rl.allow("k") is False
    now[0] += 61  # both events fall out of the window
    assert rl.allow("k") is True


def test_keys_are_independent():
    now = [0.0]
    rl = RateLimiter(max_events=1, window_seconds=10, clock=lambda: now[0])
    assert rl.allow("a") is True
    assert rl.allow("b") is True
    assert rl.allow("a") is False


def test_expired_keys_are_evicted_to_bound_memory():
    now = [0.0]
    rl = RateLimiter(max_events=1, window_seconds=10, clock=lambda: now[0])
    rl.allow("gone")
    now[0] += 11
    rl.allow("here")
    assert "gone" not in rl._events  # evicted on the sweep
