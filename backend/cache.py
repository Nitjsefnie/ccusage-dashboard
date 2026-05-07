"""In-process caches:
  - transcript LRU keyed by r2_etag, 256 MB, 20-min idle eviction
"""
from __future__ import annotations

import time
from collections import OrderedDict


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
