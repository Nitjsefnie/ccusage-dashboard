"""In-process caches:
  - transcript LRU keyed by r2_etag, 256 MB, 20-min idle eviction
  - response cache with stale-while-revalidate for the heavy read endpoints
"""
from __future__ import annotations

import functools
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger("claudit.cache")


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
    """Process-local cache with a flat TTL and generation-based staleness.

    Values are whatever the decorated endpoint returns (dicts). Not
    size-bounded — the keyspace is (endpoint × range × project × model),
    which is small.

    Ingest used to ``clear()`` this, which dropped every user onto the
    cold path once an hour — and cold means 8s+ for the dashboard. It now
    calls ``invalidate()``, which bumps a generation counter so existing
    entries read as STALE but stay servable. A stale hit is returned
    immediately and refreshed in the background; the TTL remains the hard
    limit past which an entry is dropped and must be recomputed inline.
    """

    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[Any, float, int]] = {}
        self._generation = 0
        self._guard = threading.Lock()

    def get_entry(self, key: str) -> tuple[Any, bool] | None:
        """Return ``(value, is_stale)``, or None on miss/expiry."""
        with self._guard:
            item = self._items.get(key)
            if item is None:
                return None
            value, ts, generation = item
            if time.time() - ts > self.ttl_seconds:
                self._items.pop(key, None)
                return None
            return value, generation != self._generation

    def get(self, key: str) -> Any | None:
        entry = self.get_entry(key)
        return None if entry is None else entry[0]

    def put(self, key: str, value: Any) -> None:
        with self._guard:
            self._items[key] = (value, time.time(), self._generation)

    def invalidate(self) -> None:
        """Mark every entry stale WITHOUT dropping it."""
        with self._guard:
            self._generation += 1

    def clear(self) -> None:
        with self._guard:
            self._items.clear()
            self._generation += 1


response_cache = _TTLCache(ttl_seconds=3600)


# Background refreshes for stale entries. Small pool on purpose: a refresh
# is a full uncached query, and running many at once would starve the
# connection pool that live requests need.
_refresh_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cache-refresh")
_refreshing: set[str] = set()
_key_locks: dict[str, threading.Lock] = {}
_registry_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _registry_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _schedule_refresh(key: str, fn: Callable[..., dict], kwargs: dict[str, Any]) -> None:
    """Recompute `key` off the request path, at most once concurrently."""
    with _registry_guard:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def _run() -> None:
        try:
            response_cache.put(key, fn(**kwargs))
        except Exception:
            # A failed refresh leaves the stale entry in place, which is
            # the whole point — better stale than a 500 or an 8s wait.
            log.exception("background refresh failed for %s", key)
        finally:
            with _registry_guard:
                _refreshing.discard(key)

    _refresh_pool.submit(_run)


def warm(fn: Callable[..., dict], **overrides: Any) -> None:
    """Populate the cache entry a request with `overrides` would produce.

    An ingest leaves Postgres' buffer cache cold — recompute_canonical and
    rebuild_rollup rewrite the tables — and a restart leaves the response
    cache empty on top of that, so the first visitor pays both: measured
    6.0s vs 1.4s warm for /api/dashboard. Stale-while-revalidate cannot
    help there because there is nothing stale to serve yet.

    The key MUST match what the decorated wrapper computes for a real
    request, and FastAPI passes every declared query param as a keyword.
    So start from the endpoint's own defaults and apply the overrides on
    top; guessing the kwargs would warm a key nobody ever reads.
    """
    import inspect

    target = getattr(fn, "__wrapped__", fn)
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(target).parameters.items():
        default = param.default
        # Query(...) defaults carry the real value on `.default`.
        kwargs[name] = getattr(default, "default", default)
    kwargs.update(overrides)
    if "fresh" in kwargs:
        # A truthy `fresh` bypasses the cache on both read and write, so
        # warming with it would compute the response and store nothing.
        kwargs["fresh"] = 0

    def _run() -> None:
        try:
            fn(**kwargs)
        except Exception:
            log.exception("cache warm failed for %s %r", fn.__qualname__, overrides)

    _refresh_pool.submit(_run)


def cache_response(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Cache an endpoint's dict result keyed by its keyword args.

    FastAPI calls endpoints with all params as keywords and resolves the
    signature through ``functools.wraps``' ``__wrapped__`` link, so the
    wrapper can keep a ``**kwargs`` signature while FastAPI still parses
    the original query params. A truthy ``fresh`` kwarg bypasses the
    cache (read+write) — only ``/api/dashboard`` declares ``fresh``.

    The decorated endpoints are SYNC (blocking psycopg), so this wrapper
    is sync too and FastAPI runs it in its threadpool. Three behaviours
    matter here:

    - fresh hit  → return it.
    - stale hit  → return it IMMEDIATELY and refresh in the background.
      Serving slightly-old numbers beats blocking a page load for 8s+.
    - miss       → compute inline, single-flighted per key so a burst of
      concurrent cold requests (a dashboard fires ~7 at once) runs the
      query once instead of N times.
    """

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> dict:
        if kwargs.get("fresh"):
            return fn(**kwargs)
        key = fn.__qualname__ + ":" + repr(sorted(kwargs.items()))

        entry = response_cache.get_entry(key)
        if entry is not None:
            value, is_stale = entry
            if is_stale:
                _schedule_refresh(key, fn, kwargs)
            return value

        with _lock_for(key):
            # Another thread may have populated it while we queued.
            entry = response_cache.get_entry(key)
            if entry is not None:
                return entry[0]
            result = fn(**kwargs)
            response_cache.put(key, result)
            return result

    return wrapper
