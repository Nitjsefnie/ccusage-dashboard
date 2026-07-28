"""R2 → Postgres ingest.

Per-file granularity: every *.jsonl under bucket root → one row in `files`
+ N rows in `records` (per Phase-1-deduped record). Cross-file uuid
dedup is a query-time concern.

Reparse trigger per FILE: row missing OR etag changed OR parser_version
mismatch. Orphan files (R2 key gone) are deleted. CASCADE drops records.

Per-session work spans TWO transactions:
  1. DELETE FROM records WHERE file_key=... + INSERT INTO files (UPSERT)
     + bulk INSERT INTO records — all in one transaction so a crash
     mid-loop leaves either the old state or the new state.
  2. (Implicit) The orphan delete + projects upserts also commit in
     their own scopes; partial progress is fine because the sessions
     row's etag is only updated when the per-file txn lands.
"""
from __future__ import annotations

import json
import logging
import lzma
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from backend import api, cache, db, events, parse, r2
from backend.constants import LATENCY_BUCKETS

log = logging.getLogger("claudit.ingest")

# Live ingest progress, for /health. Held in memory rather than written to
# ingest_runs: that row's counters are only filled by the final UPDATE, so for
# the minutes a full reparse takes /health could report nothing at all. Single
# process (no --workers), so the scheduler thread that mutates this and the
# request thread that reads it share an interpreter.
_PROGRESS: dict = {"phase": "idle", "done": 0, "total": 0,
                   "run_id": None, "started_at": None}
_PROGRESS_LOCK = threading.Lock()

# Only one ingest at a time. The hourly cron fires regardless of whether a
# previous run is still going, and a full reparse (PARSER_VERSION bump) takes
# ~40min — so a cron landed on top of a startup reparse and both walked the
# whole bucket: duplicate R2 GETs, duplicate parses, competing writes, and two
# sets of rollup rebuilds. The second run also built its todo list before the
# first had committed anything, so it redid work already done.
# Non-blocking: a skipped run is correct behaviour, not an error — the next
# hourly tick picks up whatever is left.
_RUN_LOCK = threading.Lock()


def progress_snapshot() -> dict:
    with _PROGRESS_LOCK:
        return dict(_PROGRESS)


def _set_progress(**kw) -> None:
    """Update live progress.

    Single-writer by construction: _RUN_LOCK admits one run at a time, so
    nothing else can interleave into this dict. That guarantee is the fix for
    the readout that showed `total` changing mid-run, `done` reaching 106% and
    then going backwards — two overlapping runs sharing one slot.
    """
    with _PROGRESS_LOCK:
        _PROGRESS.update(kw)

# What the fetch retry treats as transient. None of the boto3 failures is an
# OSError — ConnectionClosedError and EndpointConnectionError are
# BotoCoreErrors, and ClientError descends from neither — so catching
# OSError alone would miss exactly the drops this retry exists for.
# Deliberately NOT `Exception`: see FatalFetchError.
TRANSIENT_FETCH_ERRORS = (OSError, BotoCoreError, ClientError)

# A corrupt object, not a corrupt connection. r2.get_object inflates `.xz`
# keys transparently, so lzma raises from INSIDE the fetch — and every
# production object is `.xz`, which makes this the likeliest per-object
# failure there is. It is deterministic: retrying cannot un-truncate an
# upload, and treating it as a bug would abort the whole run over one bad
# file, which is the exact failure issue #2 is about.
CORRUPT_PAYLOAD_ERRORS = (lzma.LZMAError, EOFError)


class FatalFetchError(Exception):
    """A non-transient failure of an R2 GET, i.e. a bug rather than a drop.

    Routed past the per-object collector to the run-level handler on
    purpose: it is not something the next hourly run will fix, and booking
    it per object would report a code defect as a partial-data problem.
    """


# Bounded retry for the R2 GET only (see _fetch_with_retry). The tuple is
# the backoff BETWEEN attempts, so this is three attempts sleeping 0.5s then
# 1.0s: long enough to ride out a dropped connection, short enough that a
# genuinely dead object costs 1.5s rather than a run. Attempts are derived
# from the tuple so the two can never disagree.
FETCH_BACKOFF_S = (0.5, 1.0)
FETCH_ATTEMPTS = len(FETCH_BACKOFF_S) + 1

# How many failing keys the run's `error` summary names before it truncates.
# The point is a diagnosable message, not a transcript of every key.
FAILURE_KEYS_IN_SUMMARY = 5


@contextmanager
def _run_lock_nonblocking():
    """Acquire _RUN_LOCK without blocking, as a context manager.

    Yields False when another run holds the lock — a skipped run is
    correct behaviour, not an error; the next hourly tick picks up
    whatever is left.
    """
    acquired = _RUN_LOCK.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _RUN_LOCK.release()


def run_ingest(trigger: str) -> dict:
    with _run_lock_nonblocking() as acquired:
        if not acquired:
            log.warning(
                "ingest (%s) skipped: another run is still in flight", trigger
            )
            return {
                "skipped": True,
                "reason": "ingest already running",
                "trigger": trigger,
            }
        return run_ingest_locked(trigger)


def _open_run(started: datetime, trigger: str) -> int:
    """Insert the ingest_runs row, returning its id."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (started_at, trigger) VALUES (%s, %s) "
            "RETURNING id",
            (started, trigger),
        )
        run_id = cur.fetchone()[0]
        c.commit()
    return run_id


def _existing_files() -> dict:
    """file_key → (etag, parser_version) for reparse decisions."""
    with db.viz_conn() as c:
        return {
            row[0]: (row[1], row[2])
            for row in c.execute(
                "SELECT file_key, r2_etag, parser_version FROM files"
            ).fetchall()
        }


def _jsonl_suffix_len(key: str) -> int:
    """Length of the object's JSONL suffix, or 0 for non-transcripts.

    Objects may be stored plain or per-object xz-compressed; r2
    get_object/get_stream inflate `.xz` transparently. Strip the
    matched suffix so the stem (and thus is_main) is unaffected by
    compression.
    """
    if key.endswith(".jsonl.xz"):
        return len(".jsonl.xz")
    if key.endswith(".jsonl"):
        return len(".jsonl")
    return 0


def _track_project(seen_projects: dict[str, dict], project_id: str,
                   last_modified) -> None:
    """Accumulate first/last seen mtimes for one project."""
    proj = seen_projects.setdefault(project_id, {
        "project_id": project_id,
        "display_name": project_id,
        "first_seen_at": last_modified,
        "last_seen_at": last_modified,
    })
    if last_modified < proj["first_seen_at"]:
        proj["first_seen_at"] = last_modified
    if last_modified > proj["last_seen_at"]:
        proj["last_seen_at"] = last_modified


def _collect_todo(existing: dict, parser_version: str) -> tuple:
    """Walk the bucket: count objects, remember live keys, and queue the
    files whose etag/parser_version says they need (re)parsing.

    Returns (listed, todo, seen_keys); todo holds (obj, proj, stored) per
    file needing work, fetched+parsed later on a pool. project_id,
    session_id and is_main are derived from the key again in _persist.
    """
    listed = 0
    seen_keys: set[str] = set()
    seen_projects: dict[str, dict] = {}
    todo: list[tuple] = []
    for obj in r2.list_keys():
        if not _jsonl_suffix_len(obj.key):
            continue
        parts = obj.key.split("/")
        if len(parts) < 3:
            continue
        listed += 1
        seen_keys.add(obj.key)
        _track_project(seen_projects, parts[0], obj.last_modified)

        stored = existing.get(obj.key)
        need_reparse = (
            stored is None
            or stored[0] != obj.etag
            or stored[1] != parser_version
        )
        if need_reparse:
            todo.append((obj, seen_projects[parts[0]], stored))
    return listed, todo, seen_keys


def _fetch_parse_persist(todo: list[tuple], parser_version: str,
                         failed: list[tuple[str, str]]) -> tuple[int, int]:
    """Fetch+parse the queued files on a pool, persist on this thread.

    Fetch + parse is ~88% of per-file wall time and is network-bound
    (one R2 GET each), so it runs on a thread pool. Persistence stays
    on this thread: the per-file transaction boundary, and therefore
    ordering and failure semantics, are exactly as before. Work is
    submitted in bounded chunks so an 8k-file reparse does not hold
    every inflated blob in memory at once.

    Returns (inserted, reparsed).
    """
    inserted = 0
    reparsed = 0
    _set_progress(phase="parsing", total=len(todo), done=0)
    workers = worker_count()
    chunk = max(1, workers * 4)
    for start in range(0, len(todo), chunk):
        batch = todo[start:start + chunk]
        for (obj, proj, stored), parsed, exc in _resolve(
            batch, lambda it: _fetch_and_parse(it[0].key), workers
        ):
            if exc is not None:
                _record_failure(failed, obj.key, exc)
                continue
            _persist(obj, proj, parsed, parser_version)
            if stored is None:
                inserted += 1
            else:
                reparsed += 1
            _set_progress(done=inserted + reparsed)
    return inserted, reparsed


def _delete_orphans(seen_keys: set[str]) -> int:
    """Drop files rows whose R2 key is gone. CASCADE drops records."""
    _set_progress(phase="orphans")
    with db.viz_conn() as c, c.cursor() as cur:
        if seen_keys:
            cur.execute(
                "DELETE FROM files WHERE file_key != ALL(%s) RETURNING 1",
                (list(seen_keys),),
            )
        else:
            cur.execute("DELETE FROM files RETURNING 1")
        deleted = len(cur.fetchall())
        c.commit()
    return deleted


def _close_run(run_id: int, finished: datetime, listed: int, reparsed: int,
               inserted: int, deleted: int, err: str | None) -> None:
    """Write the final counters onto the ingest_runs row."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at=%s, r2_listed=%s, "
            "reparsed=%s, inserted=%s, deleted=%s, error=%s WHERE id=%s",
            (finished, listed, reparsed, inserted, deleted, err, run_id),
        )
        c.commit()


def _rebuild_derived_state() -> None:
    """Canonical flags, then the rollups that read them."""
    # Order matters: the rollup reads is_canonical.
    _set_progress(phase="canonical")
    recompute_canonical()
    _set_progress(phase="usage_rollup")
    rebuild_rollup()
    _set_progress(phase="tool_rollup")
    rebuild_tool_rollup()
    _set_progress(phase="latency_rollup")
    rebuild_latency_rollup()


def _walk_and_persist(parser_version: str,
                      failed: list[tuple[str, str]]) -> tuple[int, int, int, int]:
    """The fallible body of a run: list, fetch+parse+persist, orphan sweep.

    Returns (listed, inserted, reparsed, deleted). Exceptions propagate to
    run_ingest_locked, which books them as the run-level `fatal`.
    """
    listed, todo, seen_keys = _collect_todo(_existing_files(), parser_version)
    inserted, reparsed = _fetch_parse_persist(todo, parser_version, failed)
    deleted = _delete_orphans(seen_keys)
    return listed, inserted, reparsed, deleted


def run_ingest_locked(trigger: str) -> dict:
    started = datetime.now(timezone.utc)
    parser_version = os.environ.get("PARSER_VERSION", "1")
    run_id = _open_run(started, trigger)

    _set_progress(phase="listing", done=0, total=0,
                  run_id=run_id, started_at=started.isoformat())
    listed = inserted = reparsed = deleted = 0
    # Per-object failures (key, message). Recorded in the run's `error`, but
    # deliberately NOT used to gate anything: one dropped connection out of
    # 9,213 files is a run with a retry pending, not a failed run.
    failed: list[tuple[str, str]] = []
    # A whole-run exception, which DOES gate the post-passes below.
    fatal = None

    try:
        listed, inserted, reparsed, deleted = _walk_and_persist(
            parser_version, failed
        )
    except Exception as e:  # noqa: BLE001
        fatal = f"{type(e).__name__}: {e}"

    # `error` reports BOTH kinds of trouble, but only `fatal` gates anything.
    err = fatal if fatal is not None else failure_summary(failed)

    finished = datetime.now(timezone.utc)
    _close_run(run_id, finished, listed, reparsed, inserted, deleted, err)

    summary = {
        "id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "trigger": trigger,
        "r2_listed": listed,
        "inserted": inserted,
        "reparsed": reparsed,
        "deleted": deleted,
        "failed": len(failed),
        "error": err,
    }
    # Gated on `fatal`, NOT on `err`: the derived state describes whatever
    # `records` now holds, so skipping the rebuild because one object out of
    # a thousand could not be fetched is what leaves the rollups and
    # is_canonical describing the PREVIOUS dataset until the next clean run.
    if fatal is None:
        _rebuild_derived_state()

    # Data changed: mark the response cache stale, then notify connected
    # SSE clients so the dashboard re-fetches without a page reload.
    #
    # This used to clear() the cache outright, which meant every ingest
    # dropped every user onto the uncached path — 8s+ for the dashboard,
    # and worse for /api/cache. invalidate() keeps the entries servable
    # while marking them stale, so the refetch triggered by ingest_done
    # returns the previous numbers instantly and the fresh ones land via
    # the background refresh. Threadsafe: ingest may run in a scheduler
    # thread.
    if fatal is None and (inserted or reparsed or deleted):
        cache.response_cache.invalidate()
        events.broadcast_threadsafe("ingest_done", summary)

    if fatal is None:
        _set_progress(phase="warming")
        warm_common()
    _set_progress(phase="idle", done=0, total=0)
    return summary


def _resolve(items: list, call, workers: int) -> list[tuple]:
    """Run `call(item)` over `items`, pairing each with its result OR its
    exception instead of letting the first failure escape.

    Sequential when workers == 1, on a pool otherwise. Collecting with
    `[f.result() for f in as_completed(...)]` re-raised the worker's
    exception out of the collection step, which aborted the whole ingest
    AND discarded every already-fetched result alongside it. The two shapes
    have to behave identically, which is easiest to guarantee with one
    implementation.

    FatalFetchError is the one exception that still escapes: it means the
    fetch is broken rather than one object being unlucky, so it belongs to
    the run, not to the item.

    Returns [(item, result, None) | (item, None, exception)].
    """
    outcomes: list[tuple] = []
    if workers == 1:
        for item in items:
            try:
                outcomes.append((item, call(item), None))
            except FatalFetchError:
                raise
            except Exception as e:  # noqa: BLE001
                outcomes.append((item, None, e))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(call, item): item for item in items}
            for f in as_completed(futures):
                item = futures[f]
                try:
                    outcomes.append((item, f.result(), None))
                except FatalFetchError:
                    raise
                except Exception as e:  # noqa: BLE001
                    outcomes.append((item, None, e))
    return outcomes


def _record_failure(failed: list[tuple[str, str]], key: str,
                    exc: BaseException) -> None:
    """Book one object as failed and say so in the log."""
    failed.append((key, f"{type(exc).__name__}: {exc}"))
    log.warning(
        "ingest: %s failed after %d attempt(s): %s: %s",
        key, FETCH_ATTEMPTS, type(exc).__name__, exc,
    )


def failure_summary(failed: list[tuple[str, str]]) -> str | None:
    """One line naming how many objects failed and which, or None.

    Goes into ingest_runs.error so a partial run is visible in the admin
    view, without pretending the whole run failed.
    """
    if not failed:
        return None
    keys = [key for key, _ in failed]
    shown = ", ".join(keys[:FAILURE_KEYS_IN_SUMMARY])
    if len(keys) > FAILURE_KEYS_IN_SUMMARY:
        shown += f", ... (+{len(keys) - FAILURE_KEYS_IN_SUMMARY} more)"
    noun = "object" if len(keys) == 1 else "objects"
    return f"{len(keys)} {noun} failed after retries: {shown}"


def recompute_canonical() -> int:
    """Resolve cross-file uuid dedup into `records.is_canonical`.

    Marks exactly the row that ``DISTINCT ON (r.uuid) ... ORDER BY r.uuid,
    r.file_key`` used to pick at read time, so the read endpoints can
    filter on a boolean instead of sorting the whole table on every
    request. ``line_num`` breaks ties within a file_key, which the old
    read-time ORDER BY left arbitrary.

    Rows with a NULL uuid are legacy records kept verbatim (they were the
    UNION ALL leg), so they are always canonical.

    Runs after EVERY successful ingest, not only when files changed: a
    freshly-migrated DB has the column defaulted to TRUE across the board,
    and skipping the pass on a no-op ingest would leave duplicates
    double-counted until something happened to change. The UPDATE only
    touches rows whose flag actually flips, so a steady-state pass writes
    nothing. Returns the number of rows changed.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        cur = c.execute(
            """
            UPDATE records r
               SET is_canonical = w.canon
              FROM (
                    SELECT file_key, line_num,
                           (uuid IS NULL OR ROW_NUMBER() OVER (
                              PARTITION BY uuid ORDER BY file_key, line_num
                            ) = 1) AS canon
                      FROM records
                   ) w
             WHERE r.file_key = w.file_key
               AND r.line_num = w.line_num
               AND r.is_canonical IS DISTINCT FROM w.canon
            """
        )
        changed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    if changed:
        log.info("recompute_canonical: %d rows reflagged", changed)
    return changed


def rebuild_tool_rollup() -> int:
    """Rebuild `tool_rollup` from tool_uses + records + files.

    LEFT JOIN to records on purpose: /api/tool-usage counts every tool
    call, including ones with no matching usage record, while
    /api/tool-error-rate inner-joins records. Storing model='' for the
    unmatched ones lets a single table serve both — error-rate simply
    excludes model=''.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. Since the rebuild runs on every ingest, that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait; at
        # a few thousand rows the extra cost is noise.
        c.execute("DELETE FROM tool_rollup")
        cur = c.execute(
            """
            INSERT INTO tool_rollup (
              hour, project_id, model, tool_name, n_total, n_rated, n_error
            )
            SELECT date_trunc('hour', tu.ts)              AS hour,
                   f.project_id,
                   COALESCE(r.model, '')                  AS model,
                   tu.tool_name,
                   COUNT(*)                               AS n_total,
                   COUNT(*) FILTER (
                     WHERE tu.is_error IS NOT NULL AND r.file_key IS NOT NULL
                   )                                      AS n_rated,
                   COUNT(*) FILTER (
                     WHERE tu.is_error AND r.file_key IS NOT NULL
                   )                                      AS n_error
              FROM tool_uses tu
              JOIN files f    ON f.file_key = tu.file_key
              LEFT JOIN records r ON r.file_key = tu.file_key
                                 AND r.line_num = tu.line_num
             WHERE tu.ts IS NOT NULL
             GROUP BY 1, 2, 3, 4
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_tool_rollup: %d rows", written)
    return written


# Ranges warm_common pre-populates. Mirrors RangePicker's presets in
# src/app.jsx; anything the UI can request but this omits stays cold.
WARM_RANGES = ("all", "365d", "90d", "30d", "7d", "1d")


def rebuild_latency_rollup() -> int:
    """Rebuild `latency_rollup` for each display bucket width.

    Two passes per width — one grouped by project, one for the
    all-projects row (project_id = '') — because percentiles are not
    composable across a filter. Outlier dots are computed in the same
    pass and stored as JSONB.
    """
    written = 0
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '128MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. Since the rebuild runs on every ingest, that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait; at
        # a few thousand rows the extra cost is noise.
        c.execute("DELETE FROM latency_rollup")
        for bs in LATENCY_BUCKETS:
            for scope_expr, scope_join in (
                ("f.project_id", "JOIN files f ON f.file_key = r.file_key"),
                ("''", ""),
            ):
                cur = c.execute(
                    f"""
                    WITH src AS (
                      SELECT to_timestamp(
                               floor(EXTRACT(EPOCH FROM r.ts) / {bs}) * {bs} + {bs} / 2
                             ) AS bucket,
                             {scope_expr} AS project_id,
                             COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
                             r.ts, r.file_key, r.line_num,
                             r.reply_latency_s AS latency_s
                        FROM records r
                        {scope_join}
                       WHERE r.reply_latency_s IS NOT NULL
                    ),
                    bands AS (
                      SELECT bucket, project_id, model, COUNT(*) AS n,
                             PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY latency_s) AS p10,
                             PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_s) AS p50,
                             PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY latency_s) AS p90
                        FROM src GROUP BY 1, 2, 3
                    ),
                    ranked AS (
                      SELECT s.*,
                             b.n AS bucket_n,
                             ROW_NUMBER() OVER (PARTITION BY s.bucket, s.project_id, s.model
                                                ORDER BY s.latency_s DESC) AS rn_high,
                             ROW_NUMBER() OVER (PARTITION BY s.bucket, s.project_id, s.model
                                                ORDER BY s.latency_s ASC)  AS rn_low
                        FROM src s
                        JOIN bands b USING (bucket, project_id, model)
                       WHERE b.n >= 100
                    ),
                    picked AS (
                      SELECT bucket, project_id, model,
                             jsonb_agg(jsonb_build_object(
                               'ts', ts, 'latency_s', latency_s,
                               'file_key', file_key, 'line_num', line_num,
                               'kind', CASE WHEN rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
                                            THEN 'high' ELSE 'low' END
                             ) ORDER BY latency_s DESC) AS outliers
                        FROM ranked
                       WHERE rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
                          OR rn_low  <= GREATEST(1, CEIL(bucket_n * 0.01))
                       GROUP BY 1, 2, 3
                    )
                    INSERT INTO latency_rollup
                      (bucket_s, bucket, project_id, model, n, p10, p50, p90, outliers)
                    SELECT {bs}, b.bucket, b.project_id, b.model, b.n,
                           b.p10, b.p50, b.p90, COALESCE(p.outliers, '[]'::jsonb)
                      FROM bands b
                      LEFT JOIN picked p USING (bucket, project_id, model)
                    """
                )
                written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_latency_rollup: %d rows", written)
    return written


def warm_common() -> None:
    """Pre-populate the response cache for the views a fresh visitor hits.

    After an ingest the buffer cache is cold (the recompute and rollup
    rebuild just rewrote the tables) and after a RESTART the response
    cache is empty too, so the first load pays both — measured 6.0s vs
    1.4s warm. Stale-while-revalidate cannot cover the restart case
    because there is nothing stale to serve.

    Only the unfiltered default views are warmed. The full keyspace is
    (endpoint x range x project x model), which is far too large to
    precompute and would mostly evict itself; a project the user actually
    opens still costs one cold query, but the landing view never does.

    Runs on the cache's background pool, so ingest returns immediately.
    Disabled by CLAUDIT_WARM_CACHE=0 — the tests set that, because a warm
    outlives the fixture that created its database and its queries then
    race the teardown that drops it.
    """
    if os.environ.get("CLAUDIT_WARM_CACHE", "1").lower() in ("0", "false", "no"):
        return

    # Every range the picker offers, so no button lands on a cold query.
    # Must mirror RangePicker's preset values in src/app.jsx — a range the
    # UI can request but this does not list is a permanently cold key.
    # (This was ("all","30d","7d") back when these queries cost seconds
    # each; with the rollups they are ~0.1-1s, so covering all six is
    # cheap. "1d" is arguably the most valuable: its 5-minute buckets are
    # below the rollups' 1h gate, so it is the one range still served by
    # live queries.)
    for rng in WARM_RANGES:
        cache.warm(api.dashboard, range=rng)
        cache.warm(api.activity_heatmap, range=rng)
        cache.warm(api.tool_usage, range=rng)
        cache.warm(api.tool_error_rate, range=rng)
        cache.warm(api.reply_latency, range=rng)
        # /api/projects became range-scoped, so it needs warming per range
        # like everything else. Warming it bare took the endpoint's own
        # signature default ("30d") while the UI opens on "all", leaving
        # the one request every page load makes permanently uncached.
        cache.warm(api.list_projects, range=rng)
    log.info("warm_common: queued %d range(s)", len(WARM_RANGES))


def rebuild_rollup() -> int:
    """Rebuild `usage_rollup` from the canonical records.

    Full rebuild rather than an incremental merge: cross-file uuid dedup
    means a newly ingested FILE can demote a record that another file
    already contributed, so touched-files-only would leave stale sums
    behind. The whole table is ~1 row per (session, hour, model), which is
    a fraction of `records`, so rebuilding it costs one pass.

    Must run AFTER recompute_canonical() — it reads is_canonical.
    Returns the row count written.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE: TRUNCATE takes an ACCESS EXCLUSIVE lock for
        # the whole rebuild transaction, so every concurrent read of this
        # table blocks until it commits — measured at 2.9s for a SELECT that
        # normally takes 0.07s. Since the rebuild runs on every ingest, that
        # stalled readers hourly. DELETE takes ROW EXCLUSIVE and MVCC keeps
        # serving the previous rows until commit, so readers never wait; at
        # a few thousand rows the extra cost is noise.
        c.execute("DELETE FROM usage_rollup")
        cur = c.execute(
            """
            INSERT INTO usage_rollup (
              session_id, project_id, hour, model, is_main,
              first_ts, last_ts, requests,
              fresh_tokens, output_tokens, cache_creation_tokens,
              cache_read_tokens, eph5_tokens, eph1h_tokens, cost_usd
            )
            SELECT f.session_id,
                   f.project_id,
                   date_trunc('hour', r.ts)                    AS hour,
                   COALESCE(NULLIF(r.model, ''), 'unknown')    AS model,
                   f.is_main,
                   MIN(r.ts), MAX(r.ts), COUNT(*),
                   COALESCE(SUM(r.fresh_tokens), 0),
                   COALESCE(SUM(r.output_tokens), 0),
                   COALESCE(SUM(r.cache_creation_tokens), 0),
                   COALESCE(SUM(r.cache_read_tokens), 0),
                   COALESCE(SUM(r.eph5_tokens), 0),
                   COALESCE(SUM(r.eph1h_tokens), 0),
                   COALESCE(SUM(r.cost_usd), 0)
              FROM records r
              JOIN files f ON f.file_key = r.file_key
             WHERE r.is_canonical AND r.ts IS NOT NULL
             GROUP BY 1, 2, 3, 4, 5
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_rollup: %d rows", written)
    return written


def worker_count() -> int:
    """Fetch+parse concurrency.

    Unset or unparseable -> auto (network-bound work, so oversubscribe
    cores). An explicit number is honoured, clamped to at least 1, so
    INGEST_WORKERS=1 is a real "go sequential" switch for debugging.
    """
    auto = min(16, (os.cpu_count() or 4) * 2)
    raw = os.environ.get("INGEST_WORKERS", "").strip()
    if not raw:
        return auto
    try:
        return max(1, int(raw))
    except ValueError:
        return auto


def _fetch_with_retry(key: str) -> bytes:
    """One R2 GET, retried on transient failure. Runs on a pool thread.

    The retry lives here rather than in backend.r2 so the transcript and
    sidecar readers keep their current single-shot semantics — only the
    ingest, which walks the whole bucket in one pass, needs to ride out a
    transient drop.

    Three outcomes, because "did that fail" is three questions, not two:

    - TRANSIENT_FETCH_ERRORS — retry, then propagate so the caller books
      one per-object failure.
    - CORRUPT_PAYLOAD_ERRORS — propagate on the FIRST attempt. Also one
      per-object failure, but no retry: the bytes will not improve.
    - anything else — a bug, re-raised as FatalFetchError so the
      per-object collector does not absorb it. A TypeError inside
      get_object would otherwise become a 9,213-object "partial run" that
      slept the better part of four hours through the same bug instead of
      raising one loud traceback.
    """
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return r2.get_object(key)
        except CORRUPT_PAYLOAD_ERRORS:
            raise
        except TRANSIENT_FETCH_ERRORS:
            if attempt == FETCH_ATTEMPTS:
                raise
            log.warning(
                "ingest: fetch of %s failed (attempt %d/%d), retrying",
                key, attempt, FETCH_ATTEMPTS,
            )
            time.sleep(FETCH_BACKOFF_S[attempt - 1])
        except Exception as e:  # noqa: BLE001
            raise FatalFetchError(f"{key}: {type(e).__name__}: {e}") from e
    raise AssertionError("unreachable")  # pragma: no cover


def _fetch_and_parse(key: str) -> dict:
    """Runs on a pool thread. Touches no DB connection.

    Only the GET is retried: a parse failure is deterministic, so a second
    attempt reproduces the same error against the same bytes and buys
    nothing but delay.
    """
    return parse.parse_file(key, _fetch_with_retry(key))


def _persist(obj, proj, parsed, parser_version) -> None:
    """One file, one transaction — identical to the pre-pool behaviour."""
    parts = obj.key.split("/")
    project_id, session_id = parts[0], parts[1]
    stem = parts[-1][:-_jsonl_suffix_len(obj.key)]
    is_main = stem == session_id
    with db.viz_conn() as c, c.cursor() as cur:
        # Project upsert. first_seen_at uses LEAST so a later
        # ingest seeing an older file drags it backward.
        cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "VALUES (%(project_id)s, %(display_name)s, "
            "%(first_seen_at)s, %(last_seen_at)s) "
            "ON CONFLICT (project_id) DO UPDATE SET "
            "  display_name = EXCLUDED.display_name, "
            "  first_seen_at = LEAST(projects.first_seen_at, "
            "                        EXCLUDED.first_seen_at), "
            "  last_seen_at = GREATEST(projects.last_seen_at, "
            "                          EXCLUDED.last_seen_at)",
            proj,
        )
        # Wipe existing records for this file (we use UPSERT on
        # files so records need an explicit DELETE before the
        # bulk INSERT below).
        cur.execute(
            "DELETE FROM records WHERE file_key = %s", (obj.key,)
        )
        cur.execute(
            """
            INSERT INTO files (file_key, project_id, session_id,
              is_main, r2_etag, r2_size_bytes, r2_last_modified,
              parsed_at, parser_version, ctx_turns, turn_count,
              prompt_count, rate_limit_hits)
            VALUES (%(file_key)s, %(project_id)s, %(session_id)s,
              %(is_main)s, %(r2_etag)s, %(r2_size_bytes)s,
              %(r2_last_modified)s, %(parsed_at)s, %(parser_version)s,
              %(ctx_turns)s::jsonb, %(turn_count)s,
              %(prompt_count)s, %(rate_limit_hits)s::jsonb)
            ON CONFLICT (file_key) DO UPDATE SET
              project_id = EXCLUDED.project_id,
              session_id = EXCLUDED.session_id,
              is_main = EXCLUDED.is_main,
              r2_etag = EXCLUDED.r2_etag,
              r2_size_bytes = EXCLUDED.r2_size_bytes,
              r2_last_modified = EXCLUDED.r2_last_modified,
              parsed_at = EXCLUDED.parsed_at,
              parser_version = EXCLUDED.parser_version,
              ctx_turns = EXCLUDED.ctx_turns,
              turn_count = EXCLUDED.turn_count,
              prompt_count = EXCLUDED.prompt_count,
              rate_limit_hits = EXCLUDED.rate_limit_hits
            """,
            {
                "file_key": obj.key,
                "project_id": project_id,
                "session_id": session_id,
                "is_main": is_main,
                "r2_etag": obj.etag,
                "r2_size_bytes": obj.size,
                "r2_last_modified": obj.last_modified,
                "parsed_at": datetime.now(timezone.utc),
                "parser_version": parser_version,
                "ctx_turns": json.dumps(parsed["ctx_turns"], default=str),
                "turn_count": parsed["turn_count"],
                "prompt_count": parsed["prompt_count"],
                "rate_limit_hits": json.dumps(
                    parsed.get("rate_limit_hits", []), default=str
                ),
            },
        )
        # tool_uses cascades from files; explicit DELETE so a
        # reparse doesn't leave stale rows behind.
        cur.execute(
            "DELETE FROM tool_uses WHERE file_key = %s", (obj.key,)
        )
        if parsed.get("tool_uses"):
            cur.executemany(
                """
                INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, is_error)
                VALUES (%(file_key)s, %(line_num)s, %(idx)s, %(ts)s, %(tool_name)s, %(is_error)s)
                """,
                parsed["tool_uses"],
            )
        if parsed["records"]:
            cur.executemany(
                """
                INSERT INTO records (file_key, line_num, uuid,
                  request_id, ts, model, fresh_tokens,
                  cache_creation_tokens, cache_read_tokens,
                  output_tokens, eph5_tokens, eph1h_tokens, cost_usd,
                  text_chars, reply_latency_s)
                VALUES (%(file_key)s, %(line_num)s, %(uuid)s,
                  %(request_id)s, %(ts)s, %(model)s,
                  %(fresh_tokens)s, %(cache_creation_tokens)s,
                  %(cache_read_tokens)s, %(output_tokens)s,
                  %(eph5_tokens)s, %(eph1h_tokens)s, %(cost_usd)s,
                  %(text_chars)s, %(reply_latency_s)s)
                """,
                parsed["records"],
            )
        c.commit()
