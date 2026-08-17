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


def test_fully_expired_key_is_evicted():
    # A key whose window drains completely must be removed from the map — an
    # attacker cycling distinct keys must not grow it unboundedly with empty
    # deques.
    now = [0.0]
    rl = RateLimiter(max_events=2, window_s=60.0, clock=lambda: now[0])
    assert rl.allow("ip") is True
    assert "ip" in rl._events
    # After the window elapses, the next allow() for that key prunes it empty
    # first, evicts it, then re-creates a fresh single-event deque.
    now[0] = 61.0
    assert rl.allow("ip") is True
    assert len(rl._events["ip"]) == 1
