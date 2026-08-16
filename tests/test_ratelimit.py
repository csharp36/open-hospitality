# tests/test_ratelimit.py
from usali.ratelimit import RateLimiter


def test_allows_up_to_max_then_blocks_then_recovers():
    now = [0.0]
    rl = RateLimiter(max_events=2, window_s=60.0, clock=lambda: now[0])
    assert rl.allow("ip") is True
    assert rl.allow("ip") is True
    assert rl.allow("ip") is False
    assert rl.allow("other") is True
    now[0] = 61.0
    assert rl.allow("ip") is True
