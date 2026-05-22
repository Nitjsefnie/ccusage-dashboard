"""In-process caches:
  - transcript LRU keyed by r2_etag, 256 MB, 20-min idle eviction
"""
from __future__ import annotations

import functools
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable


class _IdleLRU:
    """LRU with size cap + idle-time eviction.

    `idle_seconds` is measured against the LAST ACCESS, not insert.
    A get() refreshes the timestamp; eviction removes anything not
    touched in the last `idle_seconds`.
    """

    def __init__(self, max_bytes: int, idle_seconds: int):
        self.max_bytes = max_bytes
        self.idle_seconds = idle_seconds
        self._items: "OrderedDict[str, tuple[bytes, float]]" = OrderedDict()
        self._size = 0

    def get(self, key: str) -> bytes | None:
        item = self._items.get(key)
        if item is None:
            return None
        data, _ts = item
        self._items[key] = (data, time.time())
        self._items.move_to_end(key)
        return data

    def put(self, key: str, data: bytes) -> None:
        self._evict_idle()
        if key in self._items:
            old_data, _ = self._items.pop(key)
            self._size -= len(old_data)
        while self._size + len(data) > self.max_bytes and self._items:
            _, (oldest_data, _) = self._items.popitem(last=False)
            self._size -= len(oldest_data)
        self._items[key] = (data, time.time())
        self._size += len(data)

    def _evict_idle(self) -> None:
        now = time.time()
        threshold = now - self.idle_seconds
        stale = [k for k, (_, ts) in self._items.items() if ts < threshold]
        for k in stale:
            data, _ = self._items.pop(k)
            self._size -= len(data)


transcript_cache = _IdleLRU(max_bytes=256 * 1024 * 1024, idle_seconds=1200)


class _TTLCache:
    """Process-local cache with a flat TTL. Values are whatever the
    decorated endpoint returns (dicts). Not size-bounded — the keyspace
    is (endpoint × range × project × model), which is small, and the
    cache is fully flushed on every ingest."""

    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        value, ts = item
        if time.time() - ts > self.ttl_seconds:
            self._items.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._items[key] = (value, time.time())

    def clear(self) -> None:
        self._items.clear()


response_cache = _TTLCache(ttl_seconds=3600)


def cache_response(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    """Cache an async endpoint's dict result keyed by its keyword args.

    FastAPI calls endpoints with all params as keywords and resolves the
    signature through ``functools.wraps``' ``__wrapped__`` link, so the
    wrapper can keep a ``**kwargs`` signature while FastAPI still parses
    the original query params. A truthy ``fresh`` kwarg bypasses the
    cache (read+write) — only ``/api/dashboard`` declares ``fresh``;
    on the other decorated endpoints the bypass never triggers and
    invalidation comes solely from the ingest-driven ``clear()``."""

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> dict:
        if kwargs.get("fresh"):
            return await fn(**kwargs)
        key = fn.__qualname__ + ":" + repr(sorted(kwargs.items()))
        hit = response_cache.get(key)
        if hit is not None:
            return hit
        result = await fn(**kwargs)
        response_cache.put(key, result)
        return result

    return wrapper
