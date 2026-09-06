"""Derived-state rebuilds: the tables ingest recomputes from `records`.

Split out of backend/ingest.py, which crossed pylint's 1000-line gate —
the same seam api.py was split along. Nothing here fetches, parses or
persists; every function reads what the ingest walk already wrote and
rewrites one derived table. They are called in order by
ingest._rebuild_derived_state(), and that order is load-bearing:
suppression removes rows the canonical pass would otherwise rank, and
every rollup reads is_canonical.

Re-exported from backend.ingest so existing callers keep working.
"""
from __future__ import annotations

import logging

from backend import db
from backend.constants import (CTX_BUCKET_MAX, CTX_BUCKET_WIDTH,
                               LATENCY_BUCKETS)

log = logging.getLogger("claudit.ingest")


def purge_suppressed() -> int:
    """Delete `records` for models listed in `suppressed_models`.

    Claude Code writes every session under one tree no matter which
    endpoint served it, so resuming a session on the other lane
    interleaves that provider's assistant entries into a transcript this
    deploy's bucket already owns. Those rows are real usage but not ours,
    and pricing them against our rate table invents a cost.

    Suppression is a DELETE rather than a read-time filter because every
    read path and every rollup already reads `records` as the truth; one
    deletion keeps them all consistent without fifteen extra predicates
    that a new endpoint could forget. `tool_uses` for the same lines go
    too, otherwise the tool panels keep counting calls whose model row is
    gone (they LEFT JOIN records, so the calls would resurface as an
    unlabelled model).

    Patterns are matched with ILIKE, so 'glm-%' covers a family and a bare
    model id still matches exactly. Runs before the canonical pass on
    EVERY ingest, not only when files changed, so adding a pattern takes
    effect on the next run. Removing one brings the rows back only on a
    reparse (bump PARSER_VERSION). Returns the number of records deleted.
    """
    with db.viz_conn() as c:
        row = c.execute("SELECT EXISTS (SELECT 1 FROM suppressed_models)"
                        ).fetchone()
        if not row or not row[0]:
            return 0
        c.execute(
            """
            DELETE FROM tool_uses tu
             USING records r
             WHERE tu.file_key = r.file_key
               AND tu.line_num = r.line_num
               AND EXISTS (SELECT 1 FROM suppressed_models s
                            WHERE r.model ILIKE s.pattern)
            """
        )
        cur = c.execute(
            """
            DELETE FROM records r
             WHERE EXISTS (SELECT 1 FROM suppressed_models s
                            WHERE r.model ILIKE s.pattern)
            """
        )
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    if deleted:
        log.info("purge_suppressed: %d records dropped", deleted)
    return deleted


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
              hour, project_id, model, tool_name, n_total, n_rated, n_error,
              lines_added, lines_deleted
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
                   )                                      AS n_error,
                   SUM(tu.lines_added)                     AS lines_added,
                   SUM(tu.lines_deleted)                   AS lines_deleted
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
                    db.sql_text(f"""
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
                    """),
                )
                written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_latency_rollup: %d rows", written)
    return written


def rebuild_ctx_cost_rollup() -> int:
    """Rebuild `ctx_cost_rollup` from records + files.

    The per-call context window is fresh + cache_creation + cache_read.
    That is the BILLING sum, which equals the window for a single-
    iteration record and over-states it for the rare multi-iteration one
    (advisor fan-out / retries, where parse._usage_ctx_input takes the
    max-of-iterations instead). Those are not stored per record, and
    measured at 0 of 2192 usage records across a four-file sample, so
    the panel accepts the over-statement rather than carry another
    column and a full reparse for it.

    The bucket expression must stay in step with constants.ctx_bucket().
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        # DELETE, not TRUNCATE -- same reasoning as rebuild_tool_rollup:
        # TRUNCATE's ACCESS EXCLUSIVE lock stalls every concurrent reader
        # for the whole rebuild transaction.
        c.execute("DELETE FROM ctx_cost_rollup")
        cur = c.execute(
            """
            INSERT INTO ctx_cost_rollup (
              hour, project_id, model, ctx_bucket, requests, cost_usd
            )
            SELECT date_trunc('hour', r.ts)  AS hour,
                   f.project_id,
                   r.model,
                   CASE WHEN r.fresh_tokens + r.cache_creation_tokens
                             + r.cache_read_tokens >= %(mx)s THEN %(mx)s
                        WHEN r.fresh_tokens + r.cache_creation_tokens
                             + r.cache_read_tokens <= 0 THEN 0
                        ELSE ((r.fresh_tokens + r.cache_creation_tokens
                               + r.cache_read_tokens) / %(w)s) * %(w)s
                   END                       AS ctx_bucket,
                   COUNT(*)                  AS requests,
                   SUM(r.cost_usd)           AS cost_usd
              FROM records r
              JOIN files f ON f.file_key = r.file_key
             WHERE r.is_canonical AND r.ts IS NOT NULL
             GROUP BY 1, 2, 3, 4
            """,
            {"mx": CTX_BUCKET_MAX, "w": CTX_BUCKET_WIDTH},
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_ctx_cost_rollup: %d rows", written)
    return written
