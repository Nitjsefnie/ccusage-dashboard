from __future__ import annotations

import time

from backend import cache as cache_mod
from backend.cache import _TTLCache, cache_response


def test_ttlcache_put_get():
    c = _TTLCache(ttl_seconds=60)
    c.put("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    assert c.get("missing") is None


def test_ttlcache_expiry():
    c = _TTLCache(ttl_seconds=0)
    c.put("k", {"v": 1})
    time.sleep(0.01)
    assert c.get("k") is None


def test_ttlcache_clear():
    c = _TTLCache(ttl_seconds=60)
    c.put("k", {"v": 1})
    c.clear()
    assert c.get("k") is None


def test_cache_response_decorator_caches_and_bypasses():
    calls = []

    @cache_response
    def endpoint(rng: str = "30d", fresh: int = 0) -> dict:
        calls.append(rng)
        return {"range": rng, "n": len(calls)}

    first = endpoint(rng="30d", fresh=0)
    second = endpoint(rng="30d", fresh=0)
    assert first == second                    # served from cache
    assert len(calls) == 1                    # body ran once

    bypass = endpoint(rng="30d", fresh=1)
    assert len(calls) == 2                    # fresh=1 skips the cache
    assert bypass["n"] == 2


def test_invalidate_serves_stale_then_refreshes():
    """Ingest marks entries stale; a stale hit is served immediately and
    the value is recomputed off the request path."""
    calls = []

    @cache_response
    def endpoint(rng: str = "30d", fresh: int = 0) -> dict:
        calls.append(rng)
        return {"n": len(calls)}

    assert endpoint(rng="30d", fresh=0) == {"n": 1}

    cache_mod.response_cache.invalidate()

    # The stale value comes back straight away — NOT a recomputed one.
    assert endpoint(rng="30d", fresh=0) == {"n": 1}

    # ...and the refresh lands in the background.
    deadline = time.time() + 5
    while time.time() < deadline and len(calls) < 2:
        time.sleep(0.02)
    assert len(calls) == 2, "background refresh never ran"
    assert endpoint(rng="30d", fresh=0) == {"n": 2}


def test_invalidate_keeps_entries_servable():
    c = _TTLCache(ttl_seconds=60)
    c.put("k", {"v": 1})
    c.invalidate()
    entry = c.get_entry("k")
    assert entry is not None
    value, is_stale = entry
    assert value == {"v": 1}   # still servable, unlike clear()
    assert is_stale is True
