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
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

log = logging.getLogger("claudit.api")

# Per-phase wall-clock for the heavy read endpoints, emitted as one log
# line per request. Gated on CLAUDIT_TIMING so it costs nothing normally,
# but stays in the tree — reconstructing these queries by hand in psql
# drifts from what the endpoint actually runs and hides everything that
# happens outside SQL (row marshalling, response serialisation).
TIMING_ON = os.environ.get("CLAUDIT_TIMING", "").lower() not in ("", "0", "false", "no")

_CLAUDIT_LOGGER = logging.getLogger("claudit")

if TIMING_ON and not _CLAUDIT_LOGGER.handlers:
    # uvicorn configures its own loggers and leaves the root logger at
    # WARNING, so a bare log.info() here would go nowhere. Attach our own
    # handler rather than depending on someone else's logging config.
    #
    # Attached to the "claudit" PARENT, not "claudit.api": ingest logs
    # under "claudit.ingest" and was silently discarded, so
    # recompute_canonical / rebuild_* / warm_common reported nothing and
    # the one place that says what the warmer is doing was invisible.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    _CLAUDIT_LOGGER.addHandler(_handler)
    _CLAUDIT_LOGGER.setLevel(logging.INFO)
    _CLAUDIT_LOGGER.propagate = False


class Phases:
    """Collect labelled phase timings and log them as a single line."""

    __slots__ = ("_name", "_marks", "_t0")

    def __init__(self, name: str) -> None:
        self._name = name
        self._marks: list[tuple[str, float]] = []
        self._t0 = time.perf_counter()

    @contextmanager
    def step(self, label: str):
        t = time.perf_counter()
        try:
            yield
        finally:
            self._marks.append((label, time.perf_counter() - t))

    def mark(self, label: str, seconds: float) -> None:
        self._marks.append((label, seconds))

    def execute(self, label: str, cur, sql: str, args: Any = None):
        """Time a single ``cursor.execute`` and record it under `label`.

        Returns the cursor, so call sites keep their trailing
        ``.fetchall()`` / ``.fetchone()`` unchanged.
        """
        t = time.perf_counter()
        try:
            return cur.execute(sql, args) if args is not None else cur.execute(sql)
        finally:
            self._marks.append((label, time.perf_counter() - t))

    def done(self, **extra: Any) -> None:
        if not TIMING_ON:
            return
        total = (time.perf_counter() - self._t0) * 1000
        parts = " ".join(f"{k}={v * 1000:.0f}ms" for k, v in self._marks)
        tail = " ".join(f"{k}={v}" for k, v in extra.items())
        log.info("TIMING %s total=%.0fms %s %s", self._name, total, parts, tail)

# --- dated-rate helpers ---------------------------------------------------
# cost_total always comes from SUM(cost_usd) — the per-record cost computed
# at ingest against that record's own timestamp. cost_buckets, by contrast,
# is re-derived from summed tokens, so it must be aggregated per rate epoch
# or it silently disagrees with the total it claims to decompose whenever a
# range straddles a dated rate change. See backend/pricing.RATE_EPOCHS.


def rate_epoch_sql(ts_column: str) -> tuple[str, list]:
    """SQL expression yielding a 0-based rate-epoch index, plus its params."""
    from backend import pricing

    cases = [
        f"(CASE WHEN {ts_column} >= %s THEN 1 ELSE 0 END)"
        for _ in pricing.RATE_EPOCHS
    ]
    expr = " + ".join(["0", *cases]) if cases else "0"
    return expr, list(pricing.RATE_EPOCHS)


def epoch_ts(index: int) -> datetime | None:
    """A timestamp lying inside rate epoch ``index``, for rate lookup."""
    from backend import pricing

    if not pricing.RATE_EPOCHS:
        return None
    if index <= 0:
        return pricing.RATE_EPOCHS[0] - timedelta(microseconds=1)
    return pricing.RATE_EPOCHS[min(index, len(pricing.RATE_EPOCHS)) - 1]


def fold_per_model(rows) -> list[dict]:
    """Fold (model, rate_epoch, ...) aggregate rows into one entry per model.

    Token counts and cost_total sum across epochs; cost_buckets are priced
    per epoch so they always reconcile with cost_total.
    """
    from backend import pricing

    acc: dict[str, dict] = {}
    for row in rows:
        model, epoch, turns, fresh, cc, cr, output, eph5, eph1h, cost = row
        model = model or "unknown"
        fresh = int(fresh or 0)
        cc = int(cc or 0)
        cr = int(cr or 0)
        output = int(output or 0)
        eph5 = int(eph5 or 0)
        eph1h = int(eph1h or 0)
        unsplit = max(0, cc - eph5 - eph1h)

        rates = pricing.rate_for(model, epoch_ts(int(epoch or 0)))
        entry = acc.setdefault(
            model,
            {
                "model": model,
                "turns": 0,
                "fresh": 0,
                "cache_create": 0,
                "cache_read": 0,
                "output": 0,
                "eph5": 0,
                "eph1h": 0,
                "cost_total": 0.0,
                "estimated_rate": pricing.resolve(model).estimated,
                "_buckets": {
                    "fresh": 0.0, "create_5m": 0.0, "create_1h": 0.0,
                    "read": 0.0, "output": 0.0,
                },
            },
        )
        entry["turns"] += int(turns or 0)
        entry["fresh"] += fresh
        entry["cache_create"] += cc
        entry["cache_read"] += cr
        entry["output"] += output
        entry["eph5"] += eph5
        entry["eph1h"] += eph1h
        entry["cost_total"] += float(cost or 0)
        b = entry["_buckets"]
        b["fresh"] += fresh * rates["fresh"] / 1_000_000
        b["create_5m"] += (eph5 + unsplit) * rates["create_5m"] / 1_000_000
        b["create_1h"] += eph1h * rates["create_1h"] / 1_000_000
        b["read"] += cr * rates["read"] / 1_000_000
        b["output"] += output * rates["output"] / 1_000_000

    out = []
    for entry in acc.values():
        buckets = entry.pop("_buckets")
        total_in = entry["fresh"] + entry["cache_create"] + entry["cache_read"]
        entry["hit_rate_pct"] = round(
            (entry["cache_read"] / total_in * 100.0) if total_in else 0.0, 1
        )
        entry["cost_total"] = round(entry["cost_total"], 4)
        entry["cost_buckets"] = {k: round(v, 4) for k, v in buckets.items()}
        out.append(entry)
    out.sort(key=lambda e: e["cost_total"], reverse=True)
    return out

from backend import cache, db, pricing, r2
from backend.cache import cache_response


router = APIRouter(prefix="/api")

# Export-PNG render plumbing -------------------------------------------------
# System python (has matplotlib + psycopg); the app .venv does not. Override
# via EXPORT_PYTHON for dev/test boxes where matplotlib lives elsewhere.
_EXPORT_PYTHON = os.environ.get("EXPORT_PYTHON", "/usr/bin/python3")
_EXPORT_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts/plots/ccusage_plot_db.py")
_EXPORT_TIMEOUT_S = 120
_export_lock = asyncio.Semaphore(1)

# Activity-heatmap timezone. Bound as a SQL parameter (never interpolated);
# Postgres tzdata makes AT TIME ZONE fully DST-aware (CET/CEST transitions).
HEATMAP_TZ = "Europe/Prague"


def _build_export_argv(rng: str, project: str | None, out_path: str) -> list[str]:
    """Construct the argv for the plot subprocess. The child inherits
    DATABASE_URL_VIZ from the environment, so the DSN is NOT passed on the
    command line (keeps credentials out of the process list)."""
    argv = [_EXPORT_PYTHON, _EXPORT_SCRIPT, "-o", out_path]
    if rng == "all":
        argv.append("--all")
    else:
        argv += ["-p", rng]
    if project:
        argv += ["--project", project]
    return argv


def _export_filename(rng: str, project: str | None) -> str:
    """Safe download filename: ccusage_<project-or-all>_<range>.png."""
    proj_slug = re.sub(r"[^A-Za-z0-9._-]", "_", project) if project else "all"
    return f"ccusage_{proj_slug}_{rng}.png"


async def _render_export(argv: list[str], out_path: str) -> None:
    """Run the plot subprocess, bounded by _EXPORT_TIMEOUT_S. Raises
    HTTPException(503) on timeout, HTTPException(500) on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXPORT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(503, "export render timed out")
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace")[-500:]
        print(f"[export] render failed (rc={proc.returncode}): {tail}", file=sys.stderr)
        raise HTTPException(500, "export render failed")


@router.get("/export")
async def export_png(
    range: str = Query("30d"),
    project: str | None = Query(None),
):
    """Render the full matplotlib dashboard PNG for the active filters.
    Logged-in only (guests are blocked in session.auth_middleware)."""
    _parse_range(range)  # validation only — raises HTTPException(400) on garbage
    if _export_lock.locked():
        raise HTTPException(503, "an export is already in progress; try again shortly")
    fd, out_path = tempfile.mkstemp(suffix=".png", prefix="claudit_export_")
    os.close(fd)
    try:
        argv = _build_export_argv(range, project, out_path)
        async with _export_lock:
            await _render_export(argv, out_path)
        with open(out_path, "rb") as fh:
            png = fh.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Content-Disposition": f'attachment; filename="{_export_filename(range, project)}"'
        },
    )


@router.get("/me")
def me(request: Request) -> dict:
    """Identity probe — frontend uses `is_guest` to decide which UI
    affordances to render."""
    return {
        "user_id": getattr(request.state, "user_id", None),
        "is_guest": bool(getattr(request.state, "is_guest", False)),
    }


@router.get("/tool-usage")
@cache_response
def tool_usage(
    range: str = Query("30d"),
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
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    # Args must match the order parameters appear in the SQL string:
    # model_join's LIKE first (it sits in the JOIN, before WHERE),
    # then since (in WHERE), then project (after).
    # tool_rollup pre-aggregates (hour, project, model, tool); see
    # ingest.rebuild_tool_rollup. Same bucket-width gate as /api/dashboard:
    # the 24h view buckets finer than an hour and takes the live path.
    args: list[Any] = []
    if bucket_s >= 3600:
        tu_from = "tool_rollup tu"
        ts_col, cnt = "tu.hour", "SUM(tu.n_total)"
        model_join = ""
        since_pred = "tu.hour >= date_trunc('hour', %s::timestamptz)"
        model_filter = "AND tu.model LIKE %s" if model else ""
        proj_filter = "AND tu.project_id = %s" if project else ""
        args.append(since)
        if project:
            args.append(project)
        if model:
            args.append(f"%{model}%")
        tail = f"{proj_filter} {model_filter}"
    else:
        tu_from = "tool_uses tu\n            JOIN files f ON f.file_key = tu.file_key"
        ts_col, cnt = "tu.ts", "COUNT(*)"
        model_join = ""
        if model:
            model_join = (
                "JOIN records r ON r.file_key = tu.file_key "
                "AND r.line_num = tu.line_num AND r.model LIKE %s"
            )
            args.append(f"%{model}%")
        since_pred = "tu.ts >= %s"
        args.append(since)
        tail = ""
        if project:
            tail = "AND f.project_id = %s"
            args.append(project)

    with db.viz_conn() as c:
        rows = c.execute(
            f"""
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
            """,
            args,
        ).fetchall()

    return {
        "range": range,
        "project": project,
        "bucket_s": bucket_s,
        "buckets": [
            {"ts": _iso(b), "tool": t, "n": int(n or 0)}
            for (b, t, n) in rows
        ],
    }


@router.get("/tool-error-rate")
@cache_response
def tool_error_rate(
    range: str = Query("30d"),
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
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    # Args appended in the order placeholders appear in the SQL string:
    # tu.ts >= %s, then f.project_id, then r.model.
    args: list[Any] = [since]
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
        sql = f"""
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
    else:
        proj_filter = ""
        if project:
            proj_filter = "AND f.project_id = %s"
            args.append(project)
        model_filter = ""
        if model:
            model_filter = "AND r.model LIKE %s"
            args.append(f"%{model}%")
        sql = f"""
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

    with db.viz_conn() as c:
        rows = c.execute(sql, args).fetchall()

    return {
        "range": range,
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
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Weekday × hour activity grid in HEATMAP_TZ local wall-clock time.

    dow is ISO (1=Mon … 7=Sun), hour 0–23. DST handled by Postgres
    tzdata via AT TIME ZONE — UTC+1 in winter (CET), UTC+2 in summer
    (CEST). Cross-file uuid dedup at read time, mirroring /api/dashboard
    (SV-PARSER-SPEC). Unlike dashboard's dedup_body, the model filter is
    applied to BOTH arms so uuid-less legacy rows also honour it."""
    delta = _parse_range(range)
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
            f"""
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
            """,
            args,
        ).fetchall()

    return {
        "range": range,
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


@router.get("/reply-latency")
@cache_response
def reply_latency(
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Per-(bucket, model) reply-latency percentiles + per-bucket
    top/bottom 1% outliers. Latency is the gap from each anchored user
    message to its assistant reply, computed at parse time
    (records.reply_latency_s). Model & project filters apply to the
    assistant record's model/project."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    proj_filter = ""
    args: list[Any] = []
    if model:
        # JOIN happens at the records level via the WHERE clause; no
        # separate join arg needed since records IS the source.
        pass
    args.append(since)
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)
    model_filter = ""
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")

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

    # Percentiles cannot be summed across buckets, so unlike the other
    # rollups this one is precomputed PER display bucket width — the
    # widths are epoch-aligned and there are only a handful
    # (ingest.LATENCY_BUCKETS). A range filter then just selects buckets.
    # project_id='' is the stored all-projects row: a project filter
    # changes the population inside each (bucket, model) group, so it
    # cannot be derived from the per-project rows.
    from backend.ingest import LATENCY_BUCKETS

    if bucket_s in LATENCY_BUCKETS:
        roll_args: list[Any] = [bucket_s, project or "", since]
        roll_model = ""
        if model:
            roll_model = "AND model LIKE %s"
            roll_args.append(f"%{model}%")
        with db.viz_conn() as c:
            rows = c.execute(
                f"""
                SELECT bucket, model, n, p10, p50, p90, outliers
                FROM latency_rollup
                WHERE bucket_s = %s
                  AND project_id = %s
                  AND bucket >= to_timestamp(
                        floor(EXTRACT(EPOCH FROM %s::timestamptz) / {bucket_s})
                        * {bucket_s} + {bucket_s} / 2.0)
                  {roll_model}
                ORDER BY bucket, model
                """,
                roll_args,
            ).fetchall()
        bands = [
            {
                "ts": _iso(b), "model": m, "n": int(n or 0),
                "p10": float(p10 or 0), "p50": float(p50 or 0), "p90": float(p90 or 0),
            }
            for (b, m, n, p10, p50, p90, _o) in rows
        ]
        outliers = [
            {
                "ts": o.get("ts"), "model": m,
                "latency_s": float(o.get("latency_s") or 0),
                "file_key": o.get("file_key"), "line": int(o.get("line_num") or 0),
            }
            for (_b, m, _n, _a, _c2, _d, olist) in rows
            for o in (olist or [])
        ]
        return {
            "range": range, "project": project, "model": model,
            "bucket_s": bucket_s, "bands": bands, "outliers": outliers,
        }

    with db.viz_conn() as c:
        bands_rows = c.execute(bands_sql, args).fetchall()
        outlier_rows = c.execute(outliers_sql, args).fetchall()

    return {
        "range": range,
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


@router.get("/events")
async def event_stream(request: Request):
    """Server-Sent Events stream. Currently emits one event:
      event: ingest_done
      data: {...summary...}
    The frontend reacts by re-fetching /api/dashboard. A 15-second
    heartbeat (':' comment line) keeps the connection alive through
    Cloudflare and other intermediaries."""
    import asyncio as _asyncio
    from backend import events as _events

    async def gen():
        q = _events.subscribe()
        shutdown = _events.shutdown_event()
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                if shutdown is not None and shutdown.is_set():
                    break
                # Race the queue, the shutdown signal, and a 15s heartbeat.
                # First-wins; everything else is cancelled.
                wait_tasks = [_asyncio.create_task(q.get())]
                if shutdown is not None:
                    wait_tasks.append(_asyncio.create_task(shutdown.wait()))
                done, pending = await _asyncio.wait(
                    wait_tasks,
                    timeout=15,
                    return_when=_asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if not done:
                    yield ": ping\n\n"
                    continue
                if shutdown is not None and shutdown.is_set():
                    break
                # Queue task finished — drain it
                first = next(iter(done))
                try:
                    payload = first.result()
                    yield payload
                except _asyncio.CancelledError:
                    break
        finally:
            _events.unsubscribe(q)

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
def list_projects(range: str = Query("30d")) -> dict:
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
    delta = _parse_range(range)
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


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


_BUCKET_CANDIDATES_S = (60, 5*60, 15*60, 30*60, 3600, 6*3600, 12*3600, 86400)


def _bucket_seconds(delta: timedelta) -> int:
    """Pick the LARGEST bucket size in [60s, 86400s] (≤ 1 day) that
    still produces ≥100 bins across the range. Mirrors the frontend's
    dashboard binMs picker; applied to every server-side bucketed
    query so 24h ranges don't get hardcoded-hourly 24 buckets."""
    span_s = max(1, int(delta.total_seconds()))
    chosen = _BUCKET_CANDIDATES_S[0]
    for b in _BUCKET_CANDIDATES_S:
        if b > 86400:
            break
        if span_s / b < 100:
            break
        chosen = b
    return chosen


def _parse_range(s: str) -> timedelta:
    """`Nd` / `Nh` parse normally. `all` returns now-epoch so callers
    that compute `since = now - delta` end up at the unix epoch — i.e.
    every row in the DB, not an arbitrary 100-year window."""
    if s == "all":
        return datetime.now(timezone.utc) - _EPOCH
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    raise HTTPException(400, f"bad range: {s!r}")


@router.get("/cache")
@cache_response
def cache_view(
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Literal replica of parse_session.py --cache output.

    Returns:
      {
        range, project,
        per_model: [{model, turns, fresh, cache_create, cache_read, output,
                     eph5, eph1h, hit_rate_pct, cost_total, cost_buckets}],
        session_total: {same shape, summed across per_model},
        top_output: [{ts, line, request_id, model, output, c_read,
                      c_create_1h, c_create_5m, fresh, cost, file_key}],
        top_cache_create: [...],
        top_cache_read:   [...]
      }

    Cross-file uuid dedup via DISTINCT ON (uuid) at query time. Records
    with NULL uuid (legacy) are kept verbatim (UNION ALL leg).
    """
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    proj_filter = ""
    model_filter = ""
    canon_args: list[Any] = [since]
    if project:
        # Semi-join rather than a JOIN: the top-N queries below select a
        # bare `file_key`, which both tables have — joining `files` in
        # would make that reference ambiguous.
        proj_filter = (
            "AND file_key IN (SELECT file_key FROM files WHERE project_id = %s)"
        )
        canon_args.append(project)
    if model:
        model_filter = "AND model LIKE %s"
        canon_args.append(f"%{model}%")

    ph = Phases("cache_view")

    # Dedup used to be a CTE prefixed onto each of the four queries below,
    # so Postgres re-ran the whole DISTINCT ON — the single most expensive
    # step — FOUR times per request (measured: 19s at range=all). It is
    # now resolved at ingest into records.is_canonical
    # (ingest.recompute_canonical), so each query just filters a boolean.
    canon_src = f"""
        FROM records
        WHERE ts >= %s AND is_canonical {proj_filter} {model_filter}
    """

    with db.viz_conn() as c:
        epoch_expr, epoch_params = rate_epoch_sql("ts")
        per_model_rows = ph.execute(
            "per_model", c, f"""
            SELECT model,
                   ({epoch_expr})              AS rate_epoch,
                   COUNT(*)                    AS turns,
                   SUM(fresh_tokens)           AS fresh,
                   SUM(cache_creation_tokens)  AS cache_create,
                   SUM(cache_read_tokens)      AS cache_read,
                   SUM(output_tokens)          AS output,
                   SUM(eph5_tokens)            AS eph5,
                   SUM(eph1h_tokens)           AS eph1h,
                   SUM(cost_usd)               AS cost_total
            {canon_src}
            GROUP BY model, rate_epoch
            ORDER BY cost_total DESC
            """,
            epoch_params + canon_args,
        ).fetchall()

        top_output = ph.execute(
            "top_output", c, f"""
            SELECT ts, line_num, request_id, model,
                   output_tokens, cache_read_tokens,
                   eph1h_tokens, eph5_tokens, fresh_tokens,
                   cost_usd, file_key
            {canon_src}
            ORDER BY output_tokens DESC
            LIMIT 10
            """,
            canon_args,
        ).fetchall()

        top_create = ph.execute(
            "top_create", c, f"""
            SELECT ts, line_num, request_id, model,
                   cache_creation_tokens, eph1h_tokens, eph5_tokens,
                   cache_read_tokens, output_tokens, fresh_tokens,
                   cost_usd, file_key
            {canon_src}
              AND cache_creation_tokens > 0
            ORDER BY cache_creation_tokens DESC
            LIMIT 10
            """,
            canon_args,
        ).fetchall()

        top_read = ph.execute(
            "top_read", c, f"""
            SELECT ts, line_num, request_id, model,
                   cache_read_tokens, eph1h_tokens, eph5_tokens,
                   output_tokens, fresh_tokens,
                   cost_usd, file_key
            {canon_src}
              AND cache_read_tokens > 0
            ORDER BY cache_read_tokens DESC
            LIMIT 10
            """,
            canon_args,
        ).fetchall()

    per_model = fold_per_model(per_model_rows)

    session_total = {
        "turns": sum(m["turns"] for m in per_model),
        "fresh": sum(m["fresh"] for m in per_model),
        "cache_create": sum(m["cache_create"] for m in per_model),
        "cache_read": sum(m["cache_read"] for m in per_model),
        "output": sum(m["output"] for m in per_model),
        "eph5": sum(m["eph5"] for m in per_model),
        "eph1h": sum(m["eph1h"] for m in per_model),
        "cost_total": round(sum(m["cost_total"] for m in per_model), 4),
        "cost_buckets": {
            k: round(sum(m["cost_buckets"][k] for m in per_model), 4)
            for k in ("fresh", "create_5m", "create_1h", "read", "output")
        },
        "estimated_rate": any(m["estimated_rate"] for m in per_model),
    }
    total_in = (
        session_total["fresh"]
        + session_total["cache_create"]
        + session_total["cache_read"]
    )
    session_total["hit_rate_pct"] = round(
        (session_total["cache_read"] / total_in * 100.0) if total_in else 0.0, 1
    )

    def _top_rows(rows, columns):
        out = []
        for row in rows:
            d = {}
            for col, v in zip(columns, row):
                if hasattr(v, "isoformat"):
                    d[col] = v.isoformat()
                elif col == "cost":
                    d[col] = float(v) if v is not None else 0.0
                elif col in ("ts", "request_id", "model", "file_key"):
                    d[col] = v
                else:
                    d[col] = int(v or 0)
            out.append(d)
        return out

    ph.done(models=len(per_model))

    return {
        "range": range,
        "project": project,
        "per_model": per_model,
        "session_total": session_total,
        "top_output": _top_rows(top_output, [
            "ts", "line", "request_id", "model",
            "output", "c_read", "c_create_1h", "c_create_5m", "fresh",
            "cost", "file_key",
        ]),
        "top_cache_create": _top_rows(top_create, [
            "ts", "line", "request_id", "model",
            "c_create", "c_create_1h", "c_create_5m", "c_read",
            "output", "fresh", "cost", "file_key",
        ]),
        "top_cache_read": _top_rows(top_read, [
            "ts", "line", "request_id", "model",
            "c_read", "c_create_1h", "c_create_5m",
            "output", "fresh", "cost", "file_key",
        ]),
    }


@router.get("/context-growth/agg")
@cache_response
def context_growth_agg(
    range: str = Query("30d"),
    project: str | None = Query(None),
) -> dict:
    """Distribution stats for context size, computed two ways:
       - per_turn: every turn across every file in scope (input distribution)
       - per_session_final: the LAST turn of each MAIN file's ctx_turns
    Returns mean, p50, p90, p99, max, n for both."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    proj_filter = ""
    args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)

    with db.viz_conn() as c:
        per_turn = c.execute(
            f"""
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
            """,
            args,
        ).fetchone()

        per_session = c.execute(
            f"""
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
            """,
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
        "range": range,
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


@router.get("/sessions/{session_id}/transcript")
def get_transcript(session_id: str) -> Response:
    """Stream raw jsonl from R2 via 20-min idle LRU. The MAIN file of the
    session is what's returned (the agent peers are visible only via the
    Inspector's per-file dropdown, future work)."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, r2_etag FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key, etag = row
    body = cache.transcript_cache.get(etag)
    if body is None:
        body = r2.get_object(file_key)
        cache.transcript_cache.put(etag, body)
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


@router.get("/sessions/{session_id}/sidecar")
def get_sidecar(
    session_id: str,
    path: str = Query(..., min_length=1),
) -> Response:
    """Path-validated sidecar fetch from R2 under the session's prefix."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key = row[0]
    session_prefix = file_key.rsplit("/", 1)[0] + "/"
    if path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(400, "bad path")
    full_key = session_prefix + path
    # Data files may be stored xz-compressed (`<name>.xz`); try the plain key
    # first, then the compressed one. r2.get_object inflates `.xz` transparently,
    # so the response body is the original bytes either way and the media type
    # still keys off the un-suffixed `path`.
    body = None
    for candidate in (full_key, full_key + ".xz"):
        try:
            body = r2.get_object(candidate)
            break
        except Exception:
            continue
    if body is None:
        raise HTTPException(404, "sidecar not found")
    media = "text/plain"
    if path.endswith(".jsonl"):
        media = "application/x-ndjson"
    elif path.endswith(".json"):
        media = "application/json"
    return Response(content=body, media_type=media)


# ---------------------------------------------------------------------------
# Legacy compatibility shims (R11). Restored frontend expects these.
# Source data lives in the new files+records tables; the response shape is
# the OLD pre-R9 shape so backendDashToShape / SessionsList work unchanged.
# ---------------------------------------------------------------------------


def _iso(v) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


@router.get("/dashboard")
def dashboard_route(
    request: Request,
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
    fresh: int = Query(0),
) -> dict:
    """Route wrapper around the cached dashboard payload.

    The cached body always carries cost_by_project (one cache entry shared
    by every caller, so `cache.warm(api.dashboard)` keeps working and a
    guest never triggers a second compute). Guests get the same payload
    MINUS that key: per-project names/costs are exactly what the guest
    gates on /api/projects and on project= exist to withhold (see
    session.auth_middleware), and /api/dashboard is guest-accessible. The
    dict comprehension copies rather than mutating so the cached object
    stays intact for later non-guest hits."""
    payload = dashboard(range=range, project=project, model=model, fresh=fresh)
    if bool(getattr(request.state, "is_guest", False)):
        payload = {k: v for k, v in payload.items() if k != "cost_by_project"}
    return payload


@cache_response
def dashboard(
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
    fresh: int = Query(0),
) -> dict:
    """Hourly aggregates + per-session burns + per-session ctx_lines.

    Cross-file uuid dedup at query time via DISTINCT ON; legacy NULL-uuid
    rows are kept verbatim. The deduped set is materialised once per
    request into a ``ON COMMIT DROP`` temp table, then read by the five
    panel queries. `model=opus-4-7` filters the deduped body so every
    panel derived from it (hourly, cost_by_model, response_sizes,
    sessions, ctx_traces) is constrained to records matching the model
    substring. cost_by_project is folded from the same rollup source as
    cost_by_model; the route wrapper strips it for guests."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    proj_filter = ""
    model_filter = ""
    # `args` feeds the later per-FILE queries, which filter on
    # f.r2_last_modified + project only — never on model. Keep it free of
    # the model argument or every one of them mis-binds its placeholders.
    args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)
    if model:
        model_filter = "AND d.model LIKE %s"

    # Cross-file uuid dedup is resolved at ingest into records.is_canonical
    # (see ingest.recompute_canonical), so reads filter a boolean instead of
    # re-sorting 296k rows per request. That also removes the ON COMMIT DROP
    # temp table this used to materialise: with dedup reduced to a WHERE
    # clause there is nothing expensive left to share between the queries,
    # and each one now scans records directly via records_canonical_ts_idx.
    canon_src = f"""
        FROM records d
        JOIN files f ON f.file_key = d.file_key
        WHERE d.ts >= %s AND d.is_canonical {proj_filter} {model_filter}
    """
    canon_args: list[Any] = [since]
    if project:
        canon_args.append(project)
    if model:
        canon_args.append(f"%{model}%")

    # Pre-aggregated source for everything that is a pure sum/count/min/max.
    # `usage_rollup` is grain (session_id, hour, model) and is rebuilt at
    # ingest, so the panels below read numbers that are already summed
    # instead of re-aggregating ~285k records on every request.
    #
    # It is only usable when the display bucket is at least an hour wide —
    # the 24h view buckets at 5 minutes, which an hourly rollup cannot
    # express. For those the live subquery below is shaped with the SAME
    # column names, so every query after this point is written once.
    use_rollup = bucket_s >= 3600
    roll_proj = "AND u.project_id = %s" if project else ""
    roll_model = "AND u.model LIKE %s" if model else ""
    if use_rollup:
        roll_from = "usage_rollup u"
        # date_trunc so the partial hour containing `since` is included
        # rather than silently dropped.
        roll_since = "u.hour >= date_trunc('hour', %s::timestamptz)"
    else:
        roll_from = """(
          SELECT f.session_id, f.project_id, f.is_main,
                 r.ts AS hour, r.ts AS first_ts, r.ts AS last_ts,
                 COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
                 1::bigint AS requests,
                 r.fresh_tokens, r.output_tokens, r.cache_creation_tokens,
                 r.cache_read_tokens, r.eph5_tokens, r.eph1h_tokens, r.cost_usd
            FROM records r
            JOIN files f ON f.file_key = r.file_key
           WHERE r.is_canonical AND r.ts IS NOT NULL
        ) u"""
        roll_since = "u.hour >= %s"
    roll_src = f"""
        FROM {roll_from}
        WHERE {roll_since} {roll_proj} {roll_model}
    """
    roll_args: list[Any] = [since]
    if project:
        roll_args.append(project)
    if model:
        roll_args.append(f"%{model}%")

    ph = Phases("dashboard")

    _t_sql = time.perf_counter()
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        hourly_rows = ph.execute("hourly", c,
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM u.hour) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS hour,
                   u.model,
                   SUM(u.fresh_tokens)      AS input_tokens,
                   SUM(u.output_tokens)     AS output_tokens,
                   SUM(u.eph5_tokens)       AS cache_5m_tokens,
                   SUM(u.eph1h_tokens)      AS cache_1h_tokens,
                   SUM(u.cache_read_tokens) AS cache_read_tokens,
                   SUM(u.cost_usd)          AS cost_usd,
                   SUM(u.requests)          AS requests,
                   COUNT(DISTINCT u.session_id) AS session_count
            {roll_src}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """
        ,
            roll_args,
        ).fetchall()

        # Percentiles are the one thing that cannot be rolled up — p50/p90
        # of a union of hours is not derivable from per-hour p50/p90 — so
        # this stays a live pass, narrowed to the text-bearing rows.
        response_sizes_rows = ph.execute("response_sizes", c,
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM d.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   COALESCE(NULLIF(d.model, ''), 'unknown') AS model,
                   COUNT(*) AS n,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY d.text_chars) AS p50,
                   PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY d.text_chars) AS p90
            {canon_src}
              AND d.text_chars > 0
            GROUP BY 1, 2
            ORDER BY 1, 2
            """
        ,
            canon_args,
        ).fetchall()

        total_sessions_row = ph.execute("total_sessions", c,
            f"""
            SELECT COUNT(DISTINCT u.session_id) AS n
            {roll_src}
            """
        ,
            roll_args,
        ).fetchone()
        total_sessions = int(total_sessions_row[0] or 0) if total_sessions_row else 0

        # Per-project range cost for the "Cost by Project" panel, folded
        # out of the same rollup source as cost_by_model (range-, project-
        # and model-filtered alike). Sorted DESC here so the top-10/Other
        # fold below is a plain slice.
        cost_by_project_rows = ph.execute("cost_by_project", c,
            f"""
            SELECT u.project_id, SUM(u.cost_usd) AS cost_usd
            {roll_src}
            GROUP BY 1
            ORDER BY 2 DESC
            """
        ,
            roll_args,
        ).fetchall()

        file_counts_args = list(args)
        file_counts_row = ph.execute("file_counts", c, 
            f"""
            -- The two EXISTS predicates were correlated subqueries
            -- evaluated once per file row (four of them, ~9.2k files).
            -- Resolve each to a set once and LEFT JOIN instead.
            WITH files_with_records AS (
              SELECT file_key FROM records GROUP BY file_key
            ),
            sessions_with_main AS (
              SELECT session_id FROM files WHERE is_main GROUP BY session_id
            )
            SELECT
              COUNT(*) FILTER (
                WHERE f.is_main AND fr.file_key IS NOT NULL
              ) AS main_w_usage,
              COUNT(*) FILTER (
                WHERE f.is_main AND fr.file_key IS NULL
              ) AS main_empty,
              COUNT(*) FILTER (WHERE NOT f.is_main) AS subagent_files,
              COUNT(DISTINCT f.session_id) FILTER (
                WHERE sm.session_id IS NULL AND fr.file_key IS NOT NULL
              ) AS subagent_only_sessions,
              -- Prompts: substantive user text msgs (instrumentation +
              -- interrupts excluded). Turns: ctx_turns boundaries that
              -- produced a usage-bearing assistant reply. Both summed
              -- across every file in scope (main + subagent) so sub-agent
              -- prompts/turns roll up alongside their parents.
              COALESCE(SUM(f.prompt_count), 0) AS total_prompts,
              COALESCE(SUM(f.turn_count),   0) AS total_turns
            FROM files f
            LEFT JOIN files_with_records fr ON fr.file_key = f.file_key
            LEFT JOIN sessions_with_main   sm ON sm.session_id = f.session_id
            WHERE f.r2_last_modified >= %s {proj_filter}
            """,
            file_counts_args,
        ).fetchone()
        main_w_usage           = int(file_counts_row[0] or 0) if file_counts_row else 0
        main_empty             = int(file_counts_row[1] or 0) if file_counts_row else 0
        subagent_files         = int(file_counts_row[2] or 0) if file_counts_row else 0
        subagent_only_sessions = int(file_counts_row[3] or 0) if file_counts_row else 0
        total_prompts          = int(file_counts_row[4] or 0) if file_counts_row else 0
        total_turns            = int(file_counts_row[5] or 0) if file_counts_row else 0

        # The rollup already carries per-(session, hour, model) sums, so the
        # dominant model is argmax(requests) over the in-range rows — exactly
        # what MODE() WITHIN GROUP computed from the raw records, without
        # re-sorting them.
        sessions_rows = ph.execute("sessions", c,
            f"""
            WITH per_session_model AS (
              SELECT u.session_id, u.model,
                     SUM(u.requests)              AS requests,
                     SUM(u.fresh_tokens)          AS input_tokens,
                     SUM(u.output_tokens)         AS output_tokens,
                     SUM(u.cache_creation_tokens) AS cache_create_tokens,
                     SUM(u.cache_read_tokens)     AS cache_read_tokens,
                     SUM(u.cost_usd)              AS cost_usd,
                     MIN(u.first_ts)              AS first_ts,
                     MAX(u.last_ts)               AS last_ts
              {roll_src}
              GROUP BY 1, 2
            )
            SELECT session_id,
                   EXTRACT(EPOCH FROM MIN(first_ts))::float AS start_ts,
                   EXTRACT(EPOCH FROM MAX(last_ts))::float  AS end_ts,
                   SUM(requests)            AS requests,
                   SUM(input_tokens)        AS input_tokens,
                   SUM(output_tokens)       AS output_tokens,
                   SUM(cache_create_tokens) AS cache_create_tokens,
                   SUM(cache_read_tokens)   AS cache_read_tokens,
                   SUM(cost_usd)            AS cost_usd,
                   -- Prefer the most-used REAL model; a session whose only
                   -- rows are sub-agent '<synthetic>' still gets a label
                   -- rather than going blank.
                   (ARRAY_AGG(model ORDER BY
                      (model NOT IN ('unknown', '<synthetic>')) DESC,
                      requests DESC))[1] AS model,
                   -- Every distinct real model the session used, so
                   -- per-model panels include a session even when the
                   -- model isn't the dominant one.
                   ARRAY_REMOVE(
                     ARRAY_AGG(DISTINCT model) FILTER (
                       WHERE model NOT IN ('unknown', '<synthetic>')
                     ),
                     NULL
                   ) AS models_used
            FROM per_session_model
            GROUP BY session_id
            ORDER BY SUM(cost_usd) DESC NULLS LAST
            LIMIT 500
            """
        ,
            roll_args,
        ).fetchall()


        # Per-FILE ctx traces — one row per main file AND per sub-agent
        # file with usage. The "Per-Session Context Growth" panel
        # treats each file as its own conversation, so a sub-agent
        # invocation surfaces under whatever model it ran on, even if
        # there's no main session file on disk.
        ctx_traces_args = list(args)
        ctx_traces_rows = ph.execute("ctx_traces", c, 
            f"""
            WITH scoped_files AS (
              SELECT f.file_key, f.session_id, f.is_main, f.ctx_turns
              FROM files f
              WHERE f.r2_last_modified >= %s {proj_filter}
                AND jsonb_array_length(f.ctx_turns) > 0
            ),
            -- Scoped to the files actually returned. Unrestricted, this
            -- ran an ordered-set aggregate over every record in the
            -- table on every request, ignoring both range and project.
            file_models AS (
              SELECT r.file_key,
                     COALESCE(
                       MODE() WITHIN GROUP (ORDER BY r.model) FILTER (
                         WHERE r.model <> '' AND r.model <> '<synthetic>'
                       ),
                       MODE() WITHIN GROUP (ORDER BY NULLIF(r.model, ''))
                     ) AS model
              FROM records r
              WHERE r.file_key IN (SELECT file_key FROM scoped_files)
              GROUP BY r.file_key
            )
            SELECT sf.file_key, sf.session_id, sf.is_main,
                   COALESCE(fm.model, '') AS model,
                   sf.ctx_turns
            FROM scoped_files sf
            LEFT JOIN file_models fm ON fm.file_key = sf.file_key
            """,
            ctx_traces_args,
        ).fetchall()

        burn_args = list(args)
        rl_args = list(args) + [since]
        rl_rows = ph.execute("rate_limit_hits", c, 
            f"""
            SELECT f.session_id, hit
            FROM files f, jsonb_array_elements(f.rate_limit_hits) AS hit
            WHERE f.r2_last_modified >= %s {proj_filter}
              AND jsonb_array_length(f.rate_limit_hits) > 0
              -- r2_last_modified is the file's mtime, not the hit's time:
              -- a file touched within range can still carry hits older
              -- than `since`, so filter on each hit's own ts too.
              --
              -- The cast is guarded, because casting a malformed ts RAISES
              -- (`invalid input syntax for type timestamp with time zone`)
              -- and the traceback would take out the WHOLE dashboard, not
              -- just this panel. Two halves to the guard:
              --   * pg_input_is_valid, not a regex — a shape test admits
              --     '2026-13-45T99:99:99Z' and '2026-02-30T00:00:00Z',
              --     which look like timestamps and still raise on cast.
              --     (PG16+; production is 17 and CI runs postgres:16.)
              --   * CASE, not `AND valid AND cast` — the planner may
              --     evaluate a bare cast before the test protecting it,
              --     while CASE's evaluation order is guaranteed.
              -- A hit that fails the test yields NULL, which fails the >=
              -- and is dropped, which is what we want for junk.
              AND (CASE WHEN pg_input_is_valid(hit->>'ts', 'timestamptz')
                        THEN (hit->>'ts')::timestamptz END) >= %s
            """,
            rl_args,
        ).fetchall()

    ph.mark("sql_total", time.perf_counter() - _t_sql)
    _t_build = time.perf_counter()

    hourly = []
    seen_hours: set[str | None] = set()
    # cost_by_model and response_sizes are folded out of these same rows
    # rather than costing their own full pass over the records.
    cost_by_model_acc: dict[str, float] = {}
    for row in hourly_rows:
        (hour, model, input_t, output_t, c5, c1h, cr, cost, reqs, sc) = row
        hour_iso = _iso(hour)
        is_first_for_hour = hour_iso not in seen_hours
        seen_hours.add(hour_iso)
        model_name = model or "unknown"
        hourly.append({
            "hour": hour_iso,
            "model": model_name,
            "input_tokens": int(input_t or 0),
            "output_tokens": int(output_t or 0),
            "cache_5m_tokens": int(c5 or 0),
            "cache_1h_tokens": int(c1h or 0),
            "cache_read_tokens": int(cr or 0),
            "cost_usd": float(cost or 0),
            "requests": int(reqs or 0),
            "session_count": int(sc or 0) if is_first_for_hour else 0,
        })
        cost_by_model_acc[model_name] = (
            cost_by_model_acc.get(model_name, 0.0) + float(cost or 0)
        )

    response_sizes = [
        {
            "ts": _iso(bucket),
            "model": m,
            "n": int(n or 0),
            "p50": float(p50 or 0),
            "p90": float(p90 or 0),
        }
        for (bucket, m, n, p50, p90) in response_sizes_rows
    ]

    cost_by_model = sorted(
        ({"model": m, "cost_usd": v} for m, v in cost_by_model_acc.items() if v > 0),
        key=lambda r: r["cost_usd"],
        reverse=True,
    )

    # Zero-cost rows are noise (the project picker drops all-time-zero
    # projects too), so they're excluded. Top 10 by range cost; the tail
    # collapses into ONE "Other (N projects)" row — a bar per project is
    # unreadable at ~70 projects.
    cost_by_project_pos = [
        {"project": p or "unknown", "cost_usd": float(cost or 0)}
        for (p, cost) in cost_by_project_rows
        if float(cost or 0) > 0
    ]
    cost_by_project = cost_by_project_pos[:10]
    if len(cost_by_project_pos) > 10:
        rest = cost_by_project_pos[10:]
        cost_by_project.append({
            "project": f"Other ({len(rest)} projects)",
            "cost_usd": sum(r["cost_usd"] for r in rest),
        })

    # ctx_turns used to be its own query, but it is a strict subset of
    # ctx_traces (main files only) — the same jsonb scan run twice.
    ctx_turns_by_session = {
        sid: turns
        for (fk, sid, is_main, mdl, turns) in ctx_traces_rows
        if is_main
    }
    sessions_out = []
    for row in sessions_rows:
        (sid, st, et, reqs, inp, out, cc, cr, cost, dom, models_used) = row
        raw_turns = ctx_turns_by_session.get(sid) or []
        # Project to {t, ctx} (input is total ctx-window: input+cc+cr).
        turns_proj = [
            {"t": i, "ctx": int(t.get("input", 0) or 0)}
            for i, t in enumerate(raw_turns)
            if isinstance(t, dict)
        ]
        # null (not 0) when ctx_turns is empty so the UI can flag the dot
        # as "ctx unknown" instead of silently falling back to a synthetic
        # duration-based size encoding (analyst spec 2026-05-07).
        ctx_at_end = turns_proj[-1]["ctx"] if turns_proj else None
        sessions_out.append({
            "session_id": sid,
            "start_ts": float(st or 0),
            "end_ts": float(et or 0),
            "requests": int(reqs or 0),
            "input_tokens": int(inp or 0),
            "output_tokens": int(out or 0),
            "cache_create_tokens": int(cc or 0),
            "cache_read_tokens": int(cr or 0),
            "cost_usd": float(cost or 0),
            "model": dom or "",
            "models_used": list(models_used or []),
            "ctx_at_end": ctx_at_end,
        })

    rate_limit_hits = []
    for sid, hit in rl_rows:
        ts_str = (hit or {}).get("ts") or ""
        if not ts_str:
            continue
        rate_limit_hits.append({
            "session_id": sid,
            "ts": ts_str,
            "content": (hit or {}).get("content", ""),
        })

    ph.mark("build", time.perf_counter() - _t_build)
    ph.done(
        hourly=len(hourly),
        sessions=len(sessions_out),
        ctx_traces=len(ctx_traces_rows),
    )

    return {
        "range": range,
        "project": project,
        "bucket_s": bucket_s,
        "hourly": hourly,
        "cost_by_model": cost_by_model,
        "cost_by_project": cost_by_project,
        "rate_limit_hits": rate_limit_hits,
        "sessions": sessions_out,
        "total_sessions": total_sessions,
        "main_w_usage": main_w_usage,
        "main_empty": main_empty,
        "subagent_files": subagent_files,
        "subagent_only_sessions": subagent_only_sessions,
        "total_prompts": total_prompts,
        "total_turns": total_turns,
        # `turns` is a FLAT array of ctx values, positionally indexed.
        # It used to be [{"t": i, "ctx": n}, ...] where `t` was just the
        # array index the consumer re-derived anyway — ~20 bytes per turn
        # instead of ~6, repeated across 54k turns. session_id/is_main are
        # dropped too: they are only used server-side above (to fold in
        # ctx_turns) and no consumer reads them off the wire.
        "ctx_traces": [
            {
                "model": model or "",
                "turns": [
                    int(t.get("input", 0) or 0)
                    for t in (turns or [])
                    if isinstance(t, dict)
                ],
            }
            for (fk, sid, is_main, model, turns) in ctx_traces_rows
        ],
        "response_sizes": response_sizes,
    }


def _aggregate_session_row(row) -> dict:
    """Shared row-builder for /api/sessions and /api/sessions/{id}."""
    (
        session_id, project_id, first_at, last_at, dur_s, req_count,
        input_t, output_t, c5, c1h, cr, cost, models_raw,
    ) = row
    models = {}
    if models_raw:
        # models_raw comes as a list of (model, count) pairs from a json_agg.
        for entry in models_raw:
            try:
                models[entry["model"]] = int(entry["count"])
            except (KeyError, TypeError, ValueError):
                continue
    return {
        "session_id": session_id,
        "project_id": project_id,
        "first_event_at": _iso(first_at),
        "last_event_at": _iso(last_at),
        "duration_s": int(dur_s or 0),
        "request_count": int(req_count or 0),
        "input_tokens": int(input_t or 0),
        "output_tokens": int(output_t or 0),
        "cache_create_5m_tokens": int(c5 or 0),
        "cache_create_1h_tokens": int(c1h or 0),
        "cache_read_tokens": int(cr or 0),
        "cost_usd": float(cost or 0),
        "models": models,
        "limit_hits": 0,
    }


@router.get("/sessions")
def list_sessions(
    project: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = Query(None),
) -> dict:
    """Paginated MAIN-file session list. Cursor = ISO ts of first_event_at
    (descending); pass the next_cursor from the prior page to continue.

    Aggregates fresh from the records table (no separate rollup). The
    `models` field is built from a sub-aggregation; `limit_hits` returns
    0 because the new schema doesn't track rate-limit hits per-session
    (the OLD column came from a removed join).
    """
    proj_filter = ""
    args: list[Any] = []
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)

    cursor_clause = ""
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"bad cursor: {cursor!r}")
        cursor_clause = "WHERE first_event_at < %s"
        cursor_arg: list[Any] = [cursor_dt]
    else:
        cursor_arg = []

    # Aggregate across ALL files of each session (main + agent-* sub-files)
    # with cross-file uuid dedup, mirroring /api/dashboard. Sub-agent
    # tokens/cost roll up into the parent session's totals; the session
    # is keyed by session_id (shared between main + its agent files).
    sql = f"""
    WITH deduped AS (
      (SELECT DISTINCT ON (r.uuid)
         r.file_key, r.uuid, r.ts, r.model,
         r.fresh_tokens, r.eph5_tokens, r.eph1h_tokens,
         r.cache_read_tokens, r.output_tokens, r.cost_usd
       FROM records r JOIN files f ON f.file_key = r.file_key
       WHERE r.uuid IS NOT NULL {proj_filter}
       ORDER BY r.uuid, r.file_key)
      UNION ALL
      (SELECT r.file_key, r.uuid, r.ts, r.model,
              r.fresh_tokens, r.eph5_tokens, r.eph1h_tokens,
              r.cache_read_tokens, r.output_tokens, r.cost_usd
       FROM records r JOIN files f ON f.file_key = r.file_key
       WHERE r.uuid IS NULL {proj_filter})
    ),
    per_session AS (
      SELECT f.session_id,
             min(f.project_id) AS project_id,
             min(d.ts) AS first_event_at,
             max(d.ts) AS last_event_at,
             EXTRACT(EPOCH FROM (max(d.ts) - min(d.ts)))::bigint AS duration_s,
             COUNT(*) AS request_count,
             SUM(d.fresh_tokens)         AS input_tokens,
             SUM(d.output_tokens)        AS output_tokens,
             SUM(d.eph5_tokens)          AS cache_create_5m_tokens,
             SUM(d.eph1h_tokens)         AS cache_create_1h_tokens,
             SUM(d.cache_read_tokens)    AS cache_read_tokens,
             SUM(d.cost_usd)             AS cost_usd,
             (SELECT json_agg(json_build_object('model', model, 'count', c))
              FROM (
                SELECT d2.model, COUNT(*) AS c
                FROM deduped d2
                JOIN files f2 ON f2.file_key = d2.file_key
                WHERE f2.session_id = f.session_id AND d2.model <> ''
                GROUP BY d2.model
              ) sub) AS models_raw
      FROM deduped d
      JOIN files f ON f.file_key = d.file_key
      GROUP BY f.session_id
    )
    SELECT * FROM per_session
    {cursor_clause}
    ORDER BY first_event_at DESC NULLS LAST
    LIMIT %s
    """

    with db.viz_conn() as c:
        rows = c.execute(sql, args + cursor_arg + [limit + 1]).fetchall()

    items = [_aggregate_session_row(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        # The cursor is the first_event_at of the NEXT page's first row,
        # which is the last item in `items` (we paged DESC).
        last_first = items[-1]["first_event_at"]
        next_cursor = last_first
    return {"items": items, "next_cursor": next_cursor}


@router.get("/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    """Single-session aggregation including ctx_trace and burn rate.

    `ctx_trace` is the canonical files.ctx_turns array reshaped to
    [{t: epoch_seconds, ctx: int}] for the OLD frontend chart code.
    `burn` is {tps, model} computed from the records table.
    `r2_key` is the MAIN file_key.
    `limit_hits` returns 0 (see /api/sessions docstring).
    """
    with db.viz_conn() as c:
        row = c.execute(
            """
            WITH per_session AS (
              SELECT f.session_id,
                     f.project_id,
                     f.file_key,
                     f.ctx_turns,
                     min(r.ts) AS first_event_at,
                     max(r.ts) AS last_event_at,
                     EXTRACT(EPOCH FROM (max(r.ts) - min(r.ts)))::bigint AS duration_s,
                     COUNT(*) AS request_count,
                     SUM(r.fresh_tokens)         AS input_tokens,
                     SUM(r.output_tokens)        AS output_tokens,
                     SUM(r.eph5_tokens)          AS cache_create_5m_tokens,
                     SUM(r.eph1h_tokens)         AS cache_create_1h_tokens,
                     SUM(r.cache_read_tokens)    AS cache_read_tokens,
                     SUM(r.cost_usd)             AS cost_usd,
                     (SELECT json_agg(json_build_object('model', model, 'count', c))
                      FROM (
                        SELECT model, COUNT(*) AS c
                        FROM records r2
                        WHERE r2.file_key = f.file_key AND r2.model <> ''
                        GROUP BY model
                      ) sub) AS models_raw,
                     (SELECT model FROM records r3
                      WHERE r3.file_key = f.file_key AND r3.model <> ''
                      GROUP BY model ORDER BY count(*) DESC LIMIT 1
                     ) AS dom_model
              FROM files f
              LEFT JOIN records r ON r.file_key = f.file_key
              WHERE f.session_id = %s AND f.is_main = TRUE
              GROUP BY f.session_id, f.project_id, f.file_key, f.ctx_turns
              LIMIT 1
            )
            SELECT * FROM per_session
            """,
            (session_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(404, "session not found")

    (
        sid, project_id, file_key, ctx_turns,
        first_at, last_at, dur_s, req_count,
        input_t, output_t, c5, c1h, cr, cost,
        models_raw, dom_model,
    ) = row

    base = _aggregate_session_row((
        sid, project_id, first_at, last_at, dur_s, req_count,
        input_t, output_t, c5, c1h, cr, cost, models_raw,
    ))

    # ctx_trace from ctx_turns (already canonical [{idx,ts,line,input,output,delta}])
    ctx_trace = []
    for t in (ctx_turns or []):
        ts_str = t.get("ts") if isinstance(t, dict) else None
        try:
            if ts_str:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                t_epoch = int(dt.timestamp())
            else:
                t_epoch = None
            ctx_val = int(t.get("input", 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if t_epoch is None:
            continue
        ctx_trace.append({"t": t_epoch, "ctx": ctx_val})

    # Burn (tps + dominant model) for this session.
    write_tokens = (
        base["input_tokens"]
        + base["cache_create_5m_tokens"]
        + base["cache_create_1h_tokens"]
    )
    span_s = max(base["duration_s"], 1)
    burn = {
        "tps": float(write_tokens) / span_s,
        "model": dom_model or "",
        "hit_5h_limit": False,
    }

    return {
        **base,
        "r2_key": file_key,
        "ctx_trace": ctx_trace,
        "burn": burn,
    }
