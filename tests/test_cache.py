from __future__ import annotations

import asyncio
import time

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
    async def endpoint(range: str = "30d", fresh: int = 0) -> dict:
        calls.append(range)
        return {"range": range, "n": len(calls)}

    first = asyncio.run(endpoint(range="30d", fresh=0))
    second = asyncio.run(endpoint(range="30d", fresh=0))
    assert first == second                    # served from cache
    assert len(calls) == 1                    # body ran once

    bypass = asyncio.run(endpoint(range="30d", fresh=1))
    assert len(calls) == 2                    # fresh=1 skips the cache
    assert bypass["n"] == 2
