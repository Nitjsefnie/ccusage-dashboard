"""Read endpoints. All gated by session.auth_middleware via path prefix /api/.

Per-FILE / per-RECORD shape (R1+R2+R3+R4):
  - /api/projects: list of projects with file_count + total_cost
  - /api/cache: literal compute_cache replica (per-model + top10 + buckets)
  - /api/sessions/{id}/transcript: raw bytes for Inspector (LRU cache)
  - /api/sessions/{id}/sidecar: path-validated sidecar fetch

Legacy compatibility shims (R11) for the restored Dashboard / SessionsList /
SessionView frontend (post-revert of R9). Sourced from new files+records
tables but returning OLD response shape:
  - /api/dashboard:        hourly aggregates + burns + ctx_lines
  - /api/sessions:         paginated session list
  - /api/sessions/{id}:    single session detail

Module layout (issue #8 split — the single file outgrew pylint's
1000-line gate): shared helpers live in backend/api_common.py; the
endpoint groups in backend/api_export.py, backend/api_dashboard.py,
backend/api_sessions.py and backend/api_cache.py each define their own
APIRouter, included below. This file keeps the panel endpoints that
were small enough to stay.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request
from starlette.responses import StreamingResponse

from backend import db, events
from backend.api_cache import router as cache_router
from backend.api_common import (
    HEATMAP_TZ, _bucket_seconds, _iso, _parse_range,
)
from backend.api_dashboard import router as dashboard_router
from backend.api_export import router as export_router
from backend.api_sessions import router as sessions_router
from backend.cache import cache_response
from backend.constants import LATENCY_BUCKETS

router = APIRouter(prefix="/api")
router.include_router(export_router)
router.include_router(dashboard_router)
router.include_router(sessions_router)
router.include_router(cache_router)


@router.get("/me")
def me(request: Request) -> dict:
    """Identity probe — frontend uses `is_guest` to decide which UI
    affordances to render."""
    return {
        "user_id": getattr(request.state, "user_id", None),
        "is_guest": bool(getattr(request.state, "is_guest", False)),
    }


def _tool_usage_source(bucket_s: int, project: str | None,
                       model: str | None, since: datetime) -> tuple:
    """(FROM, ts column, count expr, model JOIN, since predicate, tail,
    args) for the bucketed tool-usage query.

    tool_rollup pre-aggregates (hour, project, model, tool); see
    ingest.rebuild_tool_rollup. Same bucket-width gate as /api/dashboard:
    the 24h view buckets finer than an hour and takes the live path.
    Args come back in the order the placeholders appear in the SQL
    string: model_join's LIKE first (it sits in the JOIN, before WHERE),
    then since (in WHERE), then project (after).
    """
    args: list[Any] = []
    if bucket_s >= 3600:
        proj_filter = "AND tu.project_id = %s" if project else ""
        model_filter = "AND tu.model LIKE %s" if model else ""
        args.append(since)
        if project:
            args.append(project)
        if model:
            args.append(f"%{model}%")
        return (
            "tool_rollup tu", "tu.hour", "SUM(tu.n_total)", "",
            "tu.hour >= date_trunc('hour', %s::timestamptz)",
            f"{proj_filter} {model_filter}", args,
        )

    model_join = ""
    if model:
        model_join = (
            "JOIN records r ON r.file_key = tu.file_key "
            "AND r.line_num = tu.line_num AND r.model LIKE %s"
        )
        args.append(f"%{model}%")
    args.append(since)
    tail = ""
    if project:
        tail = "AND f.project_id = %s"
        args.append(project)
    return (
        "tool_uses tu\n            JOIN files f ON f.file_key = tu.file_key",
        "tu.ts", "COUNT(*)", model_join, "tu.ts >= %s", tail, args,
    )


@router.get("/tool-usage")
@cache_response
def tool_usage(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed tool-call counts. Bucket size = largest in [60s, 1d]
    that yields ≥100 bins across the range. Frontend stacks to 100%
    and promotes any tool that ever cracked top-N at any bucket.
    Tools that never make the cut land in 'Other'.

    `model=opus-4-7` filters to tool calls emitted by an assistant
    message whose record matches the model substring (joined on
    file_key + line_num)."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    tu_from, ts_col, cnt, model_join, since_pred, tail, args = (
        _tool_usage_source(bucket_s, project, model, since)
    )

    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_text(f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM {ts_col}) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   tu.tool_name AS tool,
                   {cnt}        AS n
            FROM {tu_from}
            {model_join}
            WHERE {since_pred} {tail}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """),
            args,
        ).fetchall()

    return {
        "range": rng,
        "project": project,
        "bucket_s": bucket_s,
        "buckets": [
            {"ts": _iso(b), "tool": t, "n": int(n or 0)}
            for (b, t, n) in rows
        ],
    }


def _tool_error_rate_sql(bucket_s: int, project: str | None,
                         model: str | None, args: list) -> str:
    """The bucketed (n_total, n_error) per (model, tool_name) query.

    Args appended in the order placeholders appear in the SQL string:
    tu.ts >= %s (already in `args`), then project, then model.
    """
    if bucket_s >= 3600:
        # n_rated/n_error already encode "is_error IS NOT NULL and a
        # records row matched"; model <> '' reproduces the inner join to
        # records that this endpoint used to do.
        proj_filter = ""
        if project:
            proj_filter = "AND project_id = %s"
            args.append(project)
        model_filter = ""
        if model:
            model_filter = "AND model LIKE %s"
            args.append(f"%{model}%")
        return f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM hour) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   model, tool_name AS tool,
                   SUM(n_rated) AS n_total,
                   SUM(n_error) AS n_error
            FROM tool_rollup
            WHERE hour >= date_trunc('hour', %s::timestamptz)
              AND model <> ''
              {proj_filter}
              {model_filter}
            GROUP BY 1, 2, 3
            HAVING SUM(n_rated) > 0
            ORDER BY 1, 2, 3
        """
    proj_filter = ""
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)
    model_filter = ""
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")
    return f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM tu.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   r.model      AS model,
                   tu.tool_name AS tool,
                   COUNT(*)                              AS n_total,
                   COUNT(*) FILTER (WHERE tu.is_error)   AS n_error
            FROM tool_uses tu
            JOIN records r ON r.file_key = tu.file_key AND r.line_num = tu.line_num
            JOIN files   f ON f.file_key = tu.file_key
            WHERE tu.is_error IS NOT NULL
              AND tu.ts >= %s
              {proj_filter}
              {model_filter}
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
        """


@router.get("/tool-error-rate")
@cache_response
def tool_error_rate(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed (n_total, n_error) per (model, tool_name) over settled
    tool calls only (is_error IS NOT NULL). The frontend computes
    error-rate = n_error / n_total per series and EMA-smooths the
    sequence.

    `model` is an optional model substring filter (parity with
    /api/tool-usage). Cross-file uuid dedup does NOT apply — tool_uses
    aren't keyed on records.uuid; the natural boundary is per-file."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    args: list[Any] = [since]
    sql = _tool_error_rate_sql(bucket_s, project, model, args)

    with db.viz_conn() as c:
        rows = c.execute(db.sql_text(sql), args).fetchall()

    return {
        "range": rng,
        "project": project,
        "bucket_s": bucket_s,
        "buckets": [
            {"ts": _iso(b), "model": m, "tool": t,
             "n_total": int(nt or 0), "n_error": int(ne or 0)}
            for (b, m, t, nt, ne) in rows
        ],
    }


@router.get("/activity-heatmap")
@cache_response
def activity_heatmap(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Weekday × hour activity grid in HEATMAP_TZ local wall-clock time.

    dow is ISO (1=Mon … 7=Sun), hour 0–23. DST handled by Postgres
    tzdata via AT TIME ZONE — UTC+1 in winter (CET), UTC+2 in summer
    (CEST). Cross-file uuid dedup at read time, mirroring /api/dashboard
    (SV-PARSER-SPEC). Unlike dashboard's dedup_body, the model filter is
    applied to BOTH arms so uuid-less legacy rows also honour it."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta

    # Served from usage_rollup: the grid is weekday x hour of pure
    # sums/counts, which is exactly what the rollup holds, and its `hour`
    # column is already dedup-resolved. Truncating to the hour in UTC is
    # safe for this because HEATMAP_TZ's offsets are whole hours, so the
    # local hour bucket is preserved. There is no bucket-width gate here
    # (unlike /api/dashboard) — the grid is always hourly.
    proj_filter = "AND u.project_id = %s" if project else ""
    model_filter = "AND u.model LIKE %s" if model else ""
    args: list[Any] = [HEATMAP_TZ, HEATMAP_TZ, since]
    if project:
        args.append(project)
    if model:
        args.append(f"%{model}%")

    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_text(f"""
            SELECT EXTRACT(ISODOW FROM (u.hour AT TIME ZONE %s))::int AS dow,
                   EXTRACT(HOUR   FROM (u.hour AT TIME ZONE %s))::int AS hour,
                   SUM(u.requests)      AS requests,
                   SUM(u.output_tokens) AS output_tokens,
                   SUM(u.cost_usd)      AS cost_usd
            FROM usage_rollup u
            WHERE u.hour >= date_trunc('hour', %s::timestamptz)
              {proj_filter} {model_filter}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """),
            args,
        ).fetchall()

    return {
        "range": rng,
        "tz": HEATMAP_TZ,
        "cells": [
            {
                "dow": int(dow),
                "hour": int(hour),
                "requests": int(n or 0),
                "output_tokens": int(out or 0),
                "cost_usd": float(cost or 0),
            }
            for (dow, hour, n, out, cost) in rows
        ],
    }


def _latency_filters(project: str | None, model: str | None,
                     since: datetime) -> tuple[str, str, list]:
    """(proj_filter, model_filter, args) for the live latency queries."""
    proj_filter = ""
    args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)
    model_filter = ""
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")
    return proj_filter, model_filter, args


def _latency_from_rollup(rng: str, project: str | None, model: str | None,
                         bucket_s: int, since: datetime) -> dict:
    """Percentiles cannot be summed across buckets, so unlike the other
    rollups this one is precomputed PER display bucket width — the
    widths are epoch-aligned and there are only a handful
    (constants.LATENCY_BUCKETS). A range filter then just selects
    buckets. project_id='' is the stored all-projects row: a project
    filter changes the population inside each (bucket, model) group,
    so it cannot be derived from the per-project rows."""
    roll_args: list[Any] = [bucket_s, project or "", since]
    roll_model = ""
    if model:
        roll_model = "AND model LIKE %s"
        roll_args.append(f"%{model}%")
    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_text(f"""
            SELECT bucket, model, n, p10, p50, p90, outliers
            FROM latency_rollup
            WHERE bucket_s = %s
              AND project_id = %s
              AND bucket >= to_timestamp(
                    floor(EXTRACT(EPOCH FROM %s::timestamptz) / {bucket_s})
                    * {bucket_s} + {bucket_s} / 2.0)
              {roll_model}
            ORDER BY bucket, model
            """),
            roll_args,
        ).fetchall()
    return {
        "range": rng,
        "project": project,
        "model": model,
        "bucket_s": bucket_s,
        "bands": [
            {
                "ts": _iso(b), "model": m, "n": int(n or 0),
                "p10": float(p10 or 0), "p50": float(p50 or 0), "p90": float(p90 or 0),
            }
            for (b, m, n, p10, p50, p90, _o) in rows
        ],
        "outliers": [
            {
                "ts": o.get("ts"), "model": m,
                "latency_s": float(o.get("latency_s") or 0),
                "file_key": o.get("file_key"), "line": int(o.get("line_num") or 0),
            }
            for (_b, m, _n, _a, _c2, _d, olist) in rows
            for o in (olist or [])
        ],
    }


def _latency_live(rng: str, project: str | None, model: str | None,
                  bucket_s: int, since: datetime) -> dict:
    """The live pass for bucket widths latency_rollup does not store."""
    proj_filter, model_filter, args = _latency_filters(project, model, since)

    # Bands: per-(bucket, model) percentiles.
    bands_sql = f"""
    SELECT to_timestamp(
             floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
           ) AS bucket,
           COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
           COUNT(*) AS n,
           PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY r.reply_latency_s) AS p10,
           PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY r.reply_latency_s) AS p50,
           PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY r.reply_latency_s) AS p90
    FROM records r
    JOIN files f ON f.file_key = r.file_key
    WHERE r.ts >= %s {proj_filter} {model_filter}
      AND r.reply_latency_s IS NOT NULL
    GROUP BY 1, 2
    ORDER BY 1, 2
    """

    # Outliers: top 1% slowest + bottom 1% fastest per (bucket, model)
    # bucket. Skip buckets with n < 100 — 1% of <100 is <1, so the
    # min/max would dominate and pollute the panel.
    outliers_sql = f"""
    WITH ranked AS (
      SELECT to_timestamp(
               floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
             ) AS bucket,
             COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
             r.ts                AS event_ts,
             r.file_key,
             r.line_num,
             r.reply_latency_s AS latency_s,
             COUNT(*) OVER (PARTITION BY
               to_timestamp(floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2),
               COALESCE(NULLIF(r.model, ''), 'unknown')
             ) AS bucket_n,
             ROW_NUMBER() OVER (PARTITION BY
               to_timestamp(floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2),
               COALESCE(NULLIF(r.model, ''), 'unknown')
               ORDER BY r.reply_latency_s DESC
             ) AS rn_high,
             ROW_NUMBER() OVER (PARTITION BY
               to_timestamp(floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2),
               COALESCE(NULLIF(r.model, ''), 'unknown')
               ORDER BY r.reply_latency_s ASC
             ) AS rn_low
      FROM records r
      JOIN files f ON f.file_key = r.file_key
      WHERE r.ts >= %s {proj_filter} {model_filter}
        AND r.reply_latency_s IS NOT NULL
    )
    SELECT bucket, model, event_ts, file_key, line_num, latency_s
    FROM ranked
    WHERE bucket_n >= 100
      AND (rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
        OR rn_low  <= GREATEST(1, CEIL(bucket_n * 0.01)))
    ORDER BY bucket, model, latency_s DESC
    """

    with db.viz_conn() as c:
        bands_rows = c.execute(db.sql_text(bands_sql), args).fetchall()
        outlier_rows = c.execute(db.sql_text(outliers_sql), args).fetchall()

    return {
        "range": rng,
        "project": project,
        "model": model,
        "bucket_s": bucket_s,
        "bands": [
            {
                "ts": _iso(b), "model": m, "n": int(n or 0),
                "p10": float(p10 or 0), "p50": float(p50 or 0), "p90": float(p90 or 0),
            }
            for (b, m, n, p10, p50, p90) in bands_rows
        ],
        "outliers": [
            {
                "ts": _iso(et), "model": m,
                "latency_s": float(lat or 0),
                "file_key": fk, "line": int(ln or 0),
            }
            for (b, m, et, fk, ln, lat) in outlier_rows
        ],
    }


@router.get("/reply-latency")
@cache_response
def reply_latency(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Per-(bucket, model) reply-latency percentiles + per-bucket
    top/bottom 1% outliers. Latency is the gap from each anchored user
    message to its assistant reply, computed at parse time
    (records.reply_latency_s). Model & project filters apply to the
    assistant record's model/project."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    if bucket_s in LATENCY_BUCKETS:
        return _latency_from_rollup(rng, project, model, bucket_s, since)
    return _latency_live(rng, project, model, bucket_s, since)


@router.get("/events")
async def event_stream(request: Request):
    """Server-Sent Events stream. Currently emits one event:
      event: ingest_done
      data: {...summary...}
    The frontend reacts by re-fetching /api/dashboard. A 15-second
    heartbeat (':' comment line) keeps the connection alive through
    Cloudflare and other intermediaries."""

    async def gen():
        q = events.subscribe()
        shutdown = events.shutdown_event()
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                if shutdown is not None and shutdown.is_set():
                    break
                # Race the queue, the shutdown signal, and a 15s heartbeat.
                # First-wins; everything else is cancelled.
                wait_tasks = [asyncio.create_task(q.get())]
                if shutdown is not None:
                    wait_tasks.append(asyncio.create_task(shutdown.wait()))
                done, pending = await asyncio.wait(
                    wait_tasks,
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if not done:
                    yield ": ping\n\n"
                    continue
                if shutdown is not None and shutdown.is_set():
                    break
                # Queue task finished — drain it
                first = next(iter(done), None)
                if first is None:
                    continue
                try:
                    payload = first.result()
                    yield payload
                except asyncio.CancelledError:
                    break
        finally:
            events.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/models")
def list_models() -> dict:
    """All distinct (real, non-synthetic) model strings ever recorded,
    with counts. Frontend canonicalizes via shortModelName for the
    dropdown."""
    with db.viz_conn() as c:
        rows = c.execute(
            """
            SELECT model, COUNT(*) AS n
            FROM records
            WHERE model <> '' AND model <> '<synthetic>'
            GROUP BY model
            ORDER BY 2 DESC
            """
        ).fetchall()
    return {"models": [{"model": m, "n": int(n)} for (m, n) in rows]}


@router.get("/projects")
@cache_response
def list_projects(rng: str = Query("30d", alias="range")) -> dict:
    """Per-project rollup: session_count, range-scoped cost, derived from
    files+usage_rollup. Ordered by the RANGE-scoped cost, descending, so
    the picker re-sorts as the dashboard's time range changes — the same
    `range` convention /api/dashboard takes (`_parse_range`, default
    "30d").

    Projects whose ALL-TIME cost is 0 are dropped entirely (never-cost
    projects are noise). A project with all-time cost but nothing in the
    selected range is still returned — sorted to the bottom with a cost
    of 0 — since this is a re-sort of the existing list, not a range
    filter; the ALL-TIME-zero exclusion and the RANGE-scoped ordering are
    two different aggregates and must not be conflated.

    Cost comes from usage_rollup instead of joining every record: this
    used to fan `projects x files x records` out to ~296k rows and was
    the slowest uncached call on a page load after /api/dashboard.

    The aggregates are computed in separate subqueries rather than by
    stacking two LEFT JOINs — joining files AND records first multiplied
    the file rows by their record count, so COUNT(f.file_key) reported
    67,969 files for a project that has 2,173.
    """
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    with db.viz_conn() as c:
        rows = c.execute(
            """
            SELECT p.project_id,
                   p.display_name,
                   COALESCE(fc.session_count, 0) AS session_count,
                   COALESCE(rc.range_cost, 0)    AS range_cost
            FROM projects p
            LEFT JOIN (
              SELECT project_id,
                     COUNT(DISTINCT session_id) AS session_count
              FROM files GROUP BY project_id
            ) fc ON fc.project_id = p.project_id
            JOIN (
              SELECT project_id, SUM(cost_usd) AS total_cost
              FROM usage_rollup GROUP BY project_id
              HAVING SUM(cost_usd) <> 0
            ) uc ON uc.project_id = p.project_id
            LEFT JOIN (
              SELECT project_id, SUM(cost_usd) AS range_cost
              FROM usage_rollup
              WHERE hour >= date_trunc('hour', %s::timestamptz)
              GROUP BY project_id
            ) rc ON rc.project_id = p.project_id
            ORDER BY range_cost DESC
            """,
            (since,),
        ).fetchall()
    return {
        "projects": [
            {
                "project_id": pid,
                "display_name": name,
                "session_count": int(sessions),
                "total_cost": float(cost),
            }
            for pid, name, sessions, cost in rows
        ],
    }


@router.get("/context-growth/agg")
@cache_response
def context_growth_agg(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
) -> dict:
    """Distribution stats for context size, computed two ways:
       - per_turn: every turn across every file in scope (input distribution)
       - per_session_final: the LAST turn of each MAIN file's ctx_turns
    Returns mean, p50, p90, p99, max, n for both."""
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    proj_filter = ""
    args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)

    with db.viz_conn() as c:
        per_turn = c.execute(
            db.sql_text(f"""
            SELECT
              COUNT(*) AS n,
              AVG(input_int) AS mean,
              PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY input_int) AS p50,
              PERCENTILE_CONT(0.9)  WITHIN GROUP (ORDER BY input_int) AS p90,
              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY input_int) AS p99,
              MAX(input_int) AS max
            FROM (
              SELECT ((turn->>'input')::int) AS input_int
              FROM files f, jsonb_array_elements(f.ctx_turns) AS turn
              WHERE f.r2_last_modified >= %s {proj_filter}
            ) t
            """),
            args,
        ).fetchone()

        per_session = c.execute(
            db.sql_text(f"""
            SELECT
              COUNT(*) AS n,
              AVG(final_input) AS mean,
              PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY final_input) AS p50,
              PERCENTILE_CONT(0.9)  WITHIN GROUP (ORDER BY final_input) AS p90,
              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY final_input) AS p99,
              MAX(final_input) AS max
            FROM (
              SELECT ((f.ctx_turns -> -1 ->> 'input')::int) AS final_input
              FROM files f
              WHERE f.is_main = TRUE
                AND f.r2_last_modified >= %s {proj_filter}
                AND jsonb_array_length(f.ctx_turns) > 0
            ) t
            """),
            args,
        ).fetchone()

    def _stats(row):
        if row is None:
            return {"n": 0, "mean": 0, "p50": 0, "p90": 0, "p99": 0, "max": 0}
        n, mean, p50, p90, p99, mx = row
        return {
            "n": int(n or 0),
            "mean": int(mean or 0),
            "p50": int(p50 or 0),
            "p90": int(p90 or 0),
            "p99": int(p99 or 0),
            "max": int(mx or 0),
        }

    return {
        "range": rng,
        "project": project,
        "per_turn": _stats(per_turn),
        "per_session_final": _stats(per_session),
    }


@router.get("/context-growth/session/{session_id}")
def context_growth_session(session_id: str) -> dict:
    """Per-turn array for the MAIN file of this session, mirroring
    parse_session.py:compute_context_growth output exactly."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, ctx_turns, turn_count "
            "FROM files WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key, turns, count = row
    final_ctx = 0
    if turns:
        try:
            final_ctx = int(turns[-1].get("input", 0))
        except (KeyError, IndexError, TypeError):
            final_ctx = 0
    return {
        "session_id": session_id,
        "file_key": file_key,
        "turns": turns,
        "total_turns": count,
        "final_ctx": final_ctx,
    }
