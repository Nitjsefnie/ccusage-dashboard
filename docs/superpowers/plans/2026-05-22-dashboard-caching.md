# Dashboard Load-Time Caching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make warm dashboard loads near-instant and roughly halve the cold load by caching heavy read-endpoint responses and computing the `deduped` CTE once per request.

**Architecture:** Two independent changes. (1) A process-local TTL response cache plus a `cache_response` decorator applied to the six heavy read endpoints, flushed when ingest completes. (2) `/api/dashboard` builds the `deduped` set once into a `ON COMMIT DROP` temp table with a bumped `work_mem`, instead of re-running the CTE in five separate statements.

**Tech Stack:** Python 3.13, FastAPI, psycopg3, pytest. No new dependencies.

---

## File Structure

- `backend/cache.py` — add `_TTLCache` class, `response_cache` instance, and `cache_response` decorator beside the existing transcript `_IdleLRU`.
- `backend/api.py` — decorate six endpoints; restructure `dashboard()` to use a temp table.
- `backend/ingest.py` — flush `response_cache` where `ingest_done` is broadcast.
- `tests/test_cache.py` — new unit tests for `_TTLCache` and `cache_response`.
- `tests/test_api.py` — add dashboard cache-behaviour test.
- `tests/test_ingest.py` — add cache-flush-on-ingest test.

---

## Task 1: TTL response cache + decorator

**Files:**
- Modify: `backend/cache.py`
- Test: `tests/test_cache.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cache.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_cache.py -v`
Expected: FAIL — `ImportError: cannot import name '_TTLCache'`.

- [ ] **Step 3: Implement the cache and decorator**

Append to `backend/cache.py` (after the `transcript_cache` line):

```python
import functools
from typing import Any, Awaitable, Callable


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
    cache (read+write), matching the existing browser cache-bust."""

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_cache.py -v`
Expected: PASS — all four tests.

- [ ] **Step 5: Commit**

```bash
git add backend/cache.py tests/test_cache.py
git commit -m "feat(cache): add TTL response cache and cache_response decorator

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Apply the decorator to the six heavy endpoints

**Files:**
- Modify: `backend/api.py` (decorator on lines 41, 104, 168, 429, 641, 817)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api.py` (follow the existing test style — the file already builds a fresh DB + mini R2 mirror and mounts `api.router`; reuse whatever client fixture the other tests use, referred to below as `client`):

```python
def test_dashboard_response_is_cached_and_fresh_bypasses(client):
    from backend import cache, db

    cache.response_cache.clear()
    first = client.get("/api/dashboard?range=all").json()

    # Mutate the DB underneath the cache: delete every record.
    with db.viz_conn() as c:
        c.execute("DELETE FROM records")

    cached = client.get("/api/dashboard?range=all").json()
    assert cached == first                       # stale-but-cached payload

    fresh = client.get("/api/dashboard?range=all&fresh=1").json()
    assert fresh["cost_by_model"] == []          # fresh=1 sees the empty DB
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api.py::test_dashboard_response_is_cached_and_fresh_bypasses -v`
Expected: FAIL — `cached` reflects the empty DB because the endpoint is not yet cached.

- [ ] **Step 3: Apply the decorator**

In `backend/api.py`, add `cache_response` to the import and decorate each of the six heavy endpoints. The import line currently reads:

```python
from backend import cache, db, pricing, r2
```

Add a second import line directly below it:

```python
from backend.cache import cache_response
```

Then add `@cache_response` immediately **below** each `@router.get(...)` line and **above** the `async def`, for these six endpoints:

```python
@router.get("/tool-usage")
@cache_response
async def tool_usage(
```

```python
@router.get("/tool-error-rate")
@cache_response
async def tool_error_rate(
```

```python
@router.get("/reply-latency")
@cache_response
async def reply_latency(
```

```python
@router.get("/cache")
@cache_response
async def cache_view(
```

```python
@router.get("/context-growth/agg")
@cache_response
async def context_growth_agg(
```

```python
@router.get("/dashboard")
@cache_response
async def dashboard(
```

Decorator order matters: `@router.get` must be outermost (above `@cache_response`) so FastAPI registers the wrapped function.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_api.py::test_dashboard_response_is_cached_and_fresh_bypasses -v`
Expected: PASS.

- [ ] **Step 5: Run the full API suite for regressions**

Run: `python3 -m pytest tests/test_api.py -q`
Expected: PASS — all pre-existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add backend/api.py tests/test_api.py
git commit -m "feat(api): cache heavy read-endpoint responses

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Flush the cache when ingest completes

**Files:**
- Modify: `backend/ingest.py:234`
- Test: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ingest.py` (reuse the existing ingest-run fixture/helper the other tests use to invoke `run_ingest`; below it is called as `run_ingest("test")` against the mini R2 mirror):

```python
def test_ingest_flushes_response_cache():
    from backend import cache
    from backend.ingest import run_ingest

    cache.response_cache.put("stale-key", {"v": "old"})
    assert cache.response_cache.get("stale-key") == {"v": "old"}

    run_ingest("test")

    assert cache.response_cache.get("stale-key") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ingest.py::test_ingest_flushes_response_cache -v`
Expected: FAIL — the stale key survives the ingest run.

- [ ] **Step 3: Add the flush**

In `backend/ingest.py`, line 234 currently reads:

```python
        events.broadcast_threadsafe("ingest_done", summary)
```

Add the cache flush immediately above it (same indentation):

```python
        cache.response_cache.clear()
        events.broadcast_threadsafe("ingest_done", summary)
```

If `backend/ingest.py` does not already import `cache`, add `from backend import cache` to its imports (check the existing `from backend import ...` line and extend it).

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_ingest.py::test_ingest_flushes_response_cache -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ingest.py tests/test_ingest.py
git commit -m "feat(ingest): flush response cache on ingest completion

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Compute the `deduped` set once per dashboard request

**Files:**
- Modify: `backend/api.py` — `dashboard()`, lines 846-1011
- Test: `tests/test_api.py` (existing dashboard tests are the regression guard)

This task is behaviour-preserving: the five panel queries that currently each
re-run the `deduped` CTE instead read from one temp table built once.

- [ ] **Step 1: Replace the `base_cte` definition with a `dedup_body` string**

In `backend/api.py`, the block at lines 846-866 currently reads:

```python
    base_cte = f"""
    WITH deduped AS (
      (SELECT DISTINCT ON (r.uuid)
         r.file_key, r.line_num, r.uuid, r.request_id, r.ts, r.model,
         r.fresh_tokens, r.cache_creation_tokens, r.cache_read_tokens,
         r.output_tokens, r.eph5_tokens, r.eph1h_tokens, r.cost_usd,
         r.text_chars
       FROM records r
       JOIN files f ON f.file_key = r.file_key
       WHERE r.ts >= %s {proj_filter} {model_filter} AND r.uuid IS NOT NULL
       ORDER BY r.uuid, r.file_key)
      UNION ALL
      (SELECT r.file_key, r.line_num, r.uuid, r.request_id, r.ts, r.model,
              r.fresh_tokens, r.cache_creation_tokens, r.cache_read_tokens,
              r.output_tokens, r.eph5_tokens, r.eph1h_tokens, r.cost_usd,
              r.text_chars
       FROM records r
       JOIN files f ON f.file_key = r.file_key
       WHERE r.ts >= %s {proj_filter} AND r.uuid IS NULL)
    )
    """
```

Replace it with (drop the `WITH deduped AS (` wrapper — keep only the
`UNION ALL` body):

```python
    dedup_body = f"""
      (SELECT DISTINCT ON (r.uuid)
         r.file_key, r.line_num, r.uuid, r.request_id, r.ts, r.model,
         r.fresh_tokens, r.cache_creation_tokens, r.cache_read_tokens,
         r.output_tokens, r.eph5_tokens, r.eph1h_tokens, r.cost_usd,
         r.text_chars
       FROM records r
       JOIN files f ON f.file_key = r.file_key
       WHERE r.ts >= %s {proj_filter} {model_filter} AND r.uuid IS NOT NULL
       ORDER BY r.uuid, r.file_key)
      UNION ALL
      (SELECT r.file_key, r.line_num, r.uuid, r.request_id, r.ts, r.model,
              r.fresh_tokens, r.cache_creation_tokens, r.cache_read_tokens,
              r.output_tokens, r.eph5_tokens, r.eph1h_tokens, r.cost_usd,
              r.text_chars
       FROM records r
       JOIN files f ON f.file_key = r.file_key
       WHERE r.ts >= %s {proj_filter} AND r.uuid IS NULL)
    """
```

- [ ] **Step 2: Build the temp table once inside the `viz_conn()` block**

The block currently opens at line 868:

```python
    with db.viz_conn() as c:
        hourly_rows = c.execute(
            base_cte + f"""
```

Insert the temp-table setup as the first two statements inside the `with`
block, before `hourly_rows`:

```python
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        c.execute(
            f"CREATE TEMP TABLE deduped ON COMMIT DROP AS {dedup_body}",
            args2,
        )
        hourly_rows = c.execute(
            f"""
```

(`args2` already carries the per-leg params; `viz_conn()` runs with
`autocommit=False`, so `SET LOCAL` and `ON COMMIT DROP` are transaction-scoped
and the temp table is dropped when the `with` block commits on exit.)

- [ ] **Step 3: Strip `base_cte +` and the `args2` argument from the five panel queries**

Five `c.execute(...)` calls currently prefix the SQL with `base_cte +` and pass
`args2`. Each query body already selects `FROM deduped d` — once `deduped` is a
real temp table the prefix and the argument are no longer needed. Edit each:

**3a. `hourly_rows`** (was line 869) — change `base_cte + f"""` to `f"""`, and
change the trailing `""",\n            args2,\n        ).fetchall()` to
`"""\n        ).fetchall()`. Result:

```python
        hourly_rows = c.execute(
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM d.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS hour,
                   COALESCE(NULLIF(d.model, ''), 'unknown') AS model,
                   SUM(d.fresh_tokens)     AS input_tokens,
                   SUM(d.output_tokens)    AS output_tokens,
                   SUM(d.eph5_tokens)      AS cache_5m_tokens,
                   SUM(d.eph1h_tokens)     AS cache_1h_tokens,
                   SUM(d.cache_read_tokens) AS cache_read_tokens,
                   SUM(d.cost_usd)         AS cost_usd,
                   COUNT(*)                AS requests,
                   COUNT(DISTINCT f.session_id) AS session_count
            FROM deduped d
            JOIN files f ON f.file_key = d.file_key
            WHERE d.ts IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1, 2
            """
        ).fetchall()
```

**3b. `cost_by_model_rows`** — change `base_cte + """` to `"""` and drop
`, args2`:

```python
        cost_by_model_rows = c.execute(
            """
            SELECT COALESCE(NULLIF(d.model, ''), 'unknown') AS model,
                   SUM(d.cost_usd) AS cost_usd
            FROM deduped d
            GROUP BY 1
            ORDER BY 2 DESC
            """
        ).fetchall()
```

**3c. `total_sessions_row`** — change `base_cte + """` to `"""` and drop
`, args2`:

```python
        total_sessions_row = c.execute(
            """
            SELECT COUNT(DISTINCT f.session_id) AS n
            FROM deduped d
            JOIN files f ON f.file_key = d.file_key
            WHERE d.ts IS NOT NULL
            """
        ).fetchone()
```

**3d. `sessions_rows`** — change `base_cte + """` to `"""` and drop the
trailing `, args2`. The SQL body (the `SELECT f.session_id ... LIMIT 500`
block) is unchanged; only the `base_cte +` prefix and the `args2` argument are
removed:

```python
        sessions_rows = c.execute(
            """
            SELECT f.session_id,
            ...                       # body unchanged through LIMIT 500
            """
        ).fetchall()
```

**3e. `response_sizes_rows`** — change `base_cte + f"""` to `f"""` and drop
`, args2`:

```python
        response_sizes_rows = c.execute(
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM d.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   COALESCE(NULLIF(d.model, ''), 'unknown') AS model,
                   COUNT(*) AS n,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY d.text_chars) AS p50,
                   PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY d.text_chars) AS p90
            FROM deduped d
            WHERE d.text_chars > 0 AND d.ts IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1, 2
            """
        ).fetchall()
```

Leave the non-CTE queries (`file_counts_row`, `ctx_turns_rows`,
`ctx_traces_rows`, `burn_rows`, `ctx_rows`, `rl_rows`) exactly as they are —
they do not reference `deduped`.

The local `args2` variable is still constructed and is still passed to the
`CREATE TEMP TABLE` call, so leave its definition (line 844) untouched.

- [ ] **Step 4: Run the dashboard regression tests**

Run: `python3 -m pytest tests/test_api.py -q -k dashboard`
Expected: PASS — the dashboard payload is byte-identical to before the refactor.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS — entire suite green.

- [ ] **Step 6: Commit**

```bash
git add backend/api.py
git commit -m "perf(api): build dashboard deduped set once into a temp table

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Verify against the live service

**Files:** none (manual verification)

- [ ] **Step 1: Restart the service**

Run: `systemctl restart session-viz && sleep 3 && systemctl is-active session-viz`
Expected: `active`.

- [ ] **Step 2: Confirm a cold then warm dashboard load**

Obtain an authenticated session cookie (log in via the browser or reuse an
existing cookie), then time two consecutive dashboard calls:

```bash
curl -s --connect-timeout 5 --max-time 30 -b "<cookie>" \
  -o /dev/null -w 'cold: %{time_total}s\n' \
  'http://127.0.0.1:8000/api/dashboard?range=30d'
curl -s --connect-timeout 5 --max-time 30 -b "<cookie>" \
  -o /dev/null -w 'warm: %{time_total}s\n' \
  'http://127.0.0.1:8000/api/dashboard?range=30d'
```

Expected: `cold` noticeably below the ~4.4 s baseline (~2 s); `warm` near-instant
(tens of ms).

- [ ] **Step 3: Report the measured numbers** back to the user.

---

## Self-Review Notes

- **Spec coverage:** Change 1 → Tasks 1-3; Change 2 → Task 4; expected-outcome
  verification → Task 5. All spec sections covered.
- **Out-of-scope items** (HTTP cache headers, persisted rollup, restructuring
  `/api/cache` internals) are correctly absent from every task.
- **Type consistency:** `_TTLCache`, `response_cache`, `cache_response` names
  are used identically across Tasks 1-3. `dedup_body` and `args2` consistent
  within Task 4.
