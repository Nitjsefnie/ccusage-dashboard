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
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

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


def _build_export_argv(rng: str, project: str | None, out_path: str, db_url: str) -> list[str]:
    """Construct the argv for the plot subprocess. `all` → --all, else -p <rng>."""
    argv = [_EXPORT_PYTHON, _EXPORT_SCRIPT, "--db-url", db_url, "-o", out_path]
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
    db_url = os.environ["DATABASE_URL_VIZ"]
    fd, out_path = tempfile.mkstemp(suffix=".png", prefix="ccudash_export_")
    os.close(fd)
    try:
        argv = _build_export_argv(range, project, out_path, db_url)
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
async def me(request: Request) -> dict:
    """Identity probe — frontend uses `is_guest` to decide which UI
    affordances to render."""
    return {
        "user_id": getattr(request.state, "user_id", None),
        "is_guest": bool(getattr(request.state, "is_guest", False)),
    }


@router.get("/tool-usage")
@cache_response
async def tool_usage(
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
    args: list[Any] = []
    model_join = ""
    if model:
        model_join = (
            "JOIN records r ON r.file_key = tu.file_key "
            "AND r.line_num = tu.line_num AND r.model LIKE %s"
        )
        args.append(f"%{model}%")
    args.append(since)
    proj_filter = ""
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)

    with db.viz_conn() as c:
        rows = c.execute(
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM tu.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   tu.tool_name AS tool,
                   COUNT(*)     AS n
            FROM tool_uses tu
            JOIN files f ON f.file_key = tu.file_key
            {model_join}
            WHERE tu.ts >= %s {proj_filter}
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
async def tool_error_rate(
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
    proj_filter = ""
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)
    model_filter = ""
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")

    with db.viz_conn() as c:
        rows = c.execute(
            f"""
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
            """,
            args,
        ).fetchall()

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


@router.get("/reply-latency")
@cache_response
async def reply_latency(
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

    args2 = list(args) + list(args)  # bands + outliers each take the full arg set

    with db.viz_conn() as c:
        bands_rows = c.execute(bands_sql, args).fetchall()
        outlier_rows = c.execute(outliers_sql, args).fetchall()
    _ = args2  # kept for symmetry; both queries use `args` independently

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
async def list_models() -> dict:
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
async def list_projects() -> dict:
    """Per-project rollup: file_count, total_cost, derived from files+records."""
    with db.viz_conn() as c:
        rows = c.execute(
            """
            SELECT p.project_id,
                   p.display_name,
                   COUNT(DISTINCT f.session_id) AS session_count,
                   COUNT(f.file_key)            AS file_count,
                   COALESCE(SUM(r.cost_usd), 0) AS total_cost
            FROM projects p
            LEFT JOIN files f   ON f.project_id = p.project_id
            LEFT JOIN records r ON r.file_key   = f.file_key
            GROUP BY p.project_id, p.display_name
            ORDER BY total_cost DESC
            """
        ).fetchall()
    return {
        "projects": [
            {
                "project_id": pid,
                "display_name": name,
                "session_count": int(sessions),
                "file_count": int(files),
                "total_cost": float(cost),
            }
            for pid, name, sessions, files, cost in rows
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
async def cache_view(
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
    leg_args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        leg_args.append(project)
    if model:
        model_filter = "AND r.model LIKE %s"
        leg_args.append(f"%{model}%")
    args = list(leg_args)
    args2 = leg_args + leg_args  # filters applied twice (one per UNION leg)

    base_cte = f"""
    WITH deduped AS (
      (SELECT DISTINCT ON (r.uuid) r.*
       FROM records r
       JOIN files f ON f.file_key = r.file_key
       WHERE r.ts >= %s {proj_filter} {model_filter} AND r.uuid IS NOT NULL
       ORDER BY r.uuid, r.file_key)
      UNION ALL
      (SELECT r.*
       FROM records r
       JOIN files f ON f.file_key = r.file_key
       WHERE r.ts >= %s {proj_filter} {model_filter} AND r.uuid IS NULL)
    )
    """

    with db.viz_conn() as c:
        per_model_rows = c.execute(
            base_cte + """
            SELECT model,
                   COUNT(*)                    AS turns,
                   SUM(fresh_tokens)           AS fresh,
                   SUM(cache_creation_tokens)  AS cache_create,
                   SUM(cache_read_tokens)      AS cache_read,
                   SUM(output_tokens)          AS output,
                   SUM(eph5_tokens)            AS eph5,
                   SUM(eph1h_tokens)           AS eph1h,
                   SUM(cost_usd)               AS cost_total
            FROM deduped
            GROUP BY model
            ORDER BY cost_total DESC
            """,
            args2,
        ).fetchall()

        top_output = c.execute(
            base_cte + """
            SELECT ts, line_num, request_id, model,
                   output_tokens, cache_read_tokens,
                   eph1h_tokens, eph5_tokens, fresh_tokens,
                   cost_usd, file_key
            FROM deduped
            ORDER BY output_tokens DESC
            LIMIT 10
            """,
            args2,
        ).fetchall()

        top_create = c.execute(
            base_cte + """
            SELECT ts, line_num, request_id, model,
                   cache_creation_tokens, eph1h_tokens, eph5_tokens,
                   cache_read_tokens, output_tokens, fresh_tokens,
                   cost_usd, file_key
            FROM deduped
            WHERE cache_creation_tokens > 0
            ORDER BY cache_creation_tokens DESC
            LIMIT 10
            """,
            args2,
        ).fetchall()

        top_read = c.execute(
            base_cte + """
            SELECT ts, line_num, request_id, model,
                   cache_read_tokens, eph1h_tokens, eph5_tokens,
                   output_tokens, fresh_tokens,
                   cost_usd, file_key
            FROM deduped
            WHERE cache_read_tokens > 0
            ORDER BY cache_read_tokens DESC
            LIMIT 10
            """,
            args2,
        ).fetchall()

    def _per_model(row):
        model, turns, fresh, cc, cr, output, eph5, eph1h, cost = row
        fresh = int(fresh or 0)
        cc = int(cc or 0)
        cr = int(cr or 0)
        output = int(output or 0)
        eph5 = int(eph5 or 0)
        eph1h = int(eph1h or 0)
        unsplit = max(0, cc - eph5 - eph1h)
        rates = pricing.rate_for(model)
        f_cost = fresh * rates["fresh"] / 1_000_000
        c5_cost = (eph5 + unsplit) * rates["create_5m"] / 1_000_000
        c1h_cost = eph1h * rates["create_1h"] / 1_000_000
        rd_cost = cr * rates["read"] / 1_000_000
        o_cost = output * rates["output"] / 1_000_000
        total_in = fresh + cc + cr
        return {
            "model": model,
            "turns": int(turns or 0),
            "fresh": fresh,
            "cache_create": cc,
            "cache_read": cr,
            "output": output,
            "eph5": eph5,
            "eph1h": eph1h,
            "hit_rate_pct": round((cr / total_in * 100.0) if total_in else 0.0, 1),
            "cost_total": round(float(cost or 0), 4),
            "cost_buckets": {
                "fresh": round(f_cost, 4),
                "create_5m": round(c5_cost, 4),
                "create_1h": round(c1h_cost, 4),
                "read": round(rd_cost, 4),
                "output": round(o_cost, 4),
            },
        }

    per_model = [_per_model(r) for r in per_model_rows]

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
async def context_growth_agg(
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
async def context_growth_session(session_id: str) -> dict:
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
async def get_transcript(session_id: str) -> Response:
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
async def get_sidecar(
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
    try:
        body = r2.get_object(full_key)
    except (PermissionError, FileNotFoundError):
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
@cache_response
async def dashboard(
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
    substring."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    proj_filter = ""
    model_filter = ""
    leg_args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        leg_args.append(project)
    if model:
        model_filter = "AND r.model LIKE %s"
        leg_args.append(f"%{model}%")
    args = list(leg_args)
    args2 = leg_args + leg_args  # filters applied twice (one per UNION leg)

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

    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        c.execute(
            f"CREATE TEMP TABLE deduped ON COMMIT DROP AS {dedup_body}",
            args2,
        )
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

        cost_by_model_rows = c.execute(
            """
            SELECT COALESCE(NULLIF(d.model, ''), 'unknown') AS model,
                   SUM(d.cost_usd) AS cost_usd
            FROM deduped d
            GROUP BY 1
            ORDER BY 2 DESC
            """
        ).fetchall()

        total_sessions_row = c.execute(
            """
            SELECT COUNT(DISTINCT f.session_id) AS n
            FROM deduped d
            JOIN files f ON f.file_key = d.file_key
            WHERE d.ts IS NOT NULL
            """
        ).fetchone()
        total_sessions = int(total_sessions_row[0] or 0) if total_sessions_row else 0

        file_counts_args = list(args)
        file_counts_row = c.execute(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE is_main AND EXISTS (
                SELECT 1 FROM records r WHERE r.file_key = f.file_key
              )) AS main_w_usage,
              COUNT(*) FILTER (WHERE is_main AND NOT EXISTS (
                SELECT 1 FROM records r WHERE r.file_key = f.file_key
              )) AS main_empty,
              COUNT(*) FILTER (WHERE NOT is_main) AS subagent_files,
              COUNT(DISTINCT f.session_id) FILTER (
                WHERE NOT EXISTS (
                  SELECT 1 FROM files mf
                  WHERE mf.session_id = f.session_id AND mf.is_main
                )
                AND EXISTS (
                  SELECT 1 FROM records r WHERE r.file_key = f.file_key
                )
              ) AS subagent_only_sessions,
              -- Prompts: substantive user text msgs (instrumentation +
              -- interrupts excluded). Turns: ctx_turns boundaries that
              -- produced a usage-bearing assistant reply. Both summed
              -- across every file in scope (main + subagent) so sub-agent
              -- prompts/turns roll up alongside their parents.
              COALESCE(SUM(f.prompt_count), 0) AS total_prompts,
              COALESCE(SUM(f.turn_count),   0) AS total_turns
            FROM files f
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

        sessions_rows = c.execute(
            """
            SELECT f.session_id,
                   EXTRACT(EPOCH FROM MIN(d.ts))::float AS start_ts,
                   EXTRACT(EPOCH FROM MAX(d.ts))::float AS end_ts,
                   COUNT(*) AS requests,
                   SUM(d.fresh_tokens) AS input_tokens,
                   SUM(d.output_tokens) AS output_tokens,
                   SUM(d.cache_creation_tokens) AS cache_create_tokens,
                   SUM(d.cache_read_tokens) AS cache_read_tokens,
                   SUM(d.cost_usd) AS cost_usd,
                   COALESCE(
                     -- Prefer the most-common REAL model (anything that
                     -- isn't '<synthetic>' or empty). Sessions where the
                     -- only records are sub-agent <synthetic> rows fall
                     -- back to MODE-with-synthetic so they don't go
                     -- blank — but any session with even one real model
                     -- record is labeled by that model.
                     MODE() WITHIN GROUP (ORDER BY d.model) FILTER (
                       WHERE d.model <> '' AND d.model <> '<synthetic>'
                     ),
                     MODE() WITHIN GROUP (ORDER BY NULLIF(d.model, ''))
                   ) AS model,
                   -- Every distinct (real, non-synthetic) model the
                   -- session actually used — lets per-model panels
                   -- include a session even when the model isn't the
                   -- dominant one (e.g. a session that used opus-4-5
                   -- only briefly still gets counted under opus-4-5).
                   ARRAY_REMOVE(
                     ARRAY_AGG(DISTINCT NULLIF(d.model, '')) FILTER (
                       WHERE d.model <> '' AND d.model <> '<synthetic>'
                     ),
                     NULL
                   ) AS models_used
            FROM deduped d
            JOIN files f ON f.file_key = d.file_key
            WHERE d.ts IS NOT NULL
            GROUP BY f.session_id
            ORDER BY SUM(d.cost_usd) DESC NULLS LAST
            LIMIT 500
            """
        ).fetchall()

        # Response-size time series per model — daily-bucketed
        # text_chars median and p90 of VISIBLE response content (text
        # blocks only). Per analyst (2026-05-07), output_tokens
        # silently includes thinking — and per-model thinking shares
        # vary 0.7%–25%, so token-based percentiles conflate "longer
        # responses" with "more thinking". Character count of text
        # content blocks is the clean, model-fair "visible response
        # size" measure.
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

        ctx_turns_args = list(args)
        # ctx_turns for the parent-session (main) files only — used to
        # join into per-folder `sessions_out` rows for the burn-rate
        # tooltip's ctx_at_end and the burn dot scaling.
        ctx_turns_rows = c.execute(
            f"""
            SELECT f.session_id, f.ctx_turns
            FROM files f
            WHERE f.is_main
              AND f.r2_last_modified >= %s {proj_filter}
              AND jsonb_array_length(f.ctx_turns) > 0
            """,
            ctx_turns_args,
        ).fetchall()

        # Per-FILE ctx traces — one row per main file AND per sub-agent
        # file with usage. The "Per-Session Context Growth" panel
        # treats each file as its own conversation, so a sub-agent
        # invocation surfaces under whatever model it ran on, even if
        # there's no main session file on disk.
        ctx_traces_args = list(args)
        ctx_traces_rows = c.execute(
            f"""
            WITH file_models AS (
              SELECT r.file_key,
                     COALESCE(
                       MODE() WITHIN GROUP (ORDER BY r.model) FILTER (
                         WHERE r.model <> '' AND r.model <> '<synthetic>'
                       ),
                       MODE() WITHIN GROUP (ORDER BY NULLIF(r.model, ''))
                     ) AS model
              FROM records r
              GROUP BY r.file_key
            )
            SELECT f.file_key, f.session_id, f.is_main,
                   COALESCE(fm.model, '') AS model,
                   f.ctx_turns
            FROM files f
            LEFT JOIN file_models fm ON fm.file_key = f.file_key
            WHERE f.r2_last_modified >= %s {proj_filter}
              AND jsonb_array_length(f.ctx_turns) > 0
            """,
            ctx_traces_args,
        ).fetchall()

        burn_args = list(args)
        burn_rows = c.execute(
            f"""
            WITH per_session AS (
              SELECT f.session_id, f.file_key,
                     SUM(r.fresh_tokens + r.eph5_tokens + r.eph1h_tokens) AS write_tokens,
                     EXTRACT(EPOCH FROM (max(r.ts) - min(r.ts))) AS span_s
              FROM files f
              JOIN records r ON r.file_key = f.file_key
              WHERE f.is_main AND r.ts >= %s {proj_filter}
              GROUP BY f.session_id, f.file_key
            ),
            dom_model AS (
              SELECT f.session_id,
                     (SELECT model FROM records r2
                      WHERE r2.file_key = f.file_key AND r2.model <> ''
                      GROUP BY model ORDER BY count(*) DESC LIMIT 1) AS model
              FROM files f WHERE f.is_main
            )
            SELECT ps.session_id,
                   (ps.write_tokens / GREATEST(ps.span_s, 1.0))::float AS tps,
                   COALESCE(dm.model, '') AS model
            FROM per_session ps
            LEFT JOIN dom_model dm ON dm.session_id = ps.session_id
            ORDER BY tps DESC NULLS LAST
            LIMIT 200
            """,
            burn_args,
        ).fetchall()

        ctx_args = list(args)
        ctx_rows = c.execute(
            f"""
            SELECT f.session_id, f.ctx_turns
            FROM files f
            WHERE f.is_main
              AND f.r2_last_modified >= %s {proj_filter}
              AND jsonb_array_length(f.ctx_turns) > 0
            ORDER BY (
              SELECT COALESCE(SUM(cost_usd), 0) FROM records r
              WHERE r.file_key = f.file_key
            ) DESC
            LIMIT 20
            """,
            ctx_args,
        ).fetchall()

        rl_args = list(args) + [since]
        rl_rows = c.execute(
            f"""
            SELECT f.session_id, hit
            FROM files f, jsonb_array_elements(f.rate_limit_hits) AS hit
            WHERE f.r2_last_modified >= %s {proj_filter}
              AND jsonb_array_length(f.rate_limit_hits) > 0
              -- r2_last_modified is the file's mtime, not the hit's time:
              -- a file touched within range can still carry hits older
              -- than `since`, so filter on each hit's own ts too.
              AND NULLIF(hit->>'ts', '')::timestamptz >= %s
            """,
            rl_args,
        ).fetchall()

    hourly = []
    seen_hours: set[str | None] = set()
    for row in hourly_rows:
        (hour, model, input_t, output_t, c5, c1h, cr, cost, reqs, sc) = row
        hour_iso = _iso(hour)
        is_first_for_hour = hour_iso not in seen_hours
        seen_hours.add(hour_iso)
        hourly.append({
            "hour": hour_iso,
            "model": model or "unknown",
            "input_tokens": int(input_t or 0),
            "output_tokens": int(output_t or 0),
            "cache_5m_tokens": int(c5 or 0),
            "cache_1h_tokens": int(c1h or 0),
            "cache_read_tokens": int(cr or 0),
            "cost_usd": float(cost or 0),
            "requests": int(reqs or 0),
            "session_count": int(sc or 0) if is_first_for_hour else 0,
        })

    burns = []
    for sid, tps, model in burn_rows:
        burns.append({
            "session_id": sid,
            "tps": float(tps or 0),
            "model": model or "",
            "hit_5h_limit": False,
        })

    def _parse_iso_to_epoch(s):
        try:
            if not s:
                return None
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    ctx_lines = []
    for sid, turns in ctx_rows:
        trace = []
        for t in (turns or []):
            try:
                ts_epoch = _parse_iso_to_epoch(t.get("ts"))
                ctx_val = int(t.get("input", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if ts_epoch is None:
                continue
            trace.append({"t": ts_epoch, "ctx": ctx_val})
        if trace:
            ctx_lines.append({"session_id": sid, "trace": trace})

    cost_by_model = [
        {"model": m, "cost_usd": float(c or 0)}
        for (m, c) in cost_by_model_rows
        if (c or 0) > 0
    ]

    ctx_turns_by_session = {sid: turns for (sid, turns) in ctx_turns_rows}
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
            "turns": turns_proj,
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

    return {
        "range": range,
        "project": project,
        "bucket_s": bucket_s,
        "hourly": hourly,
        "cost_by_model": cost_by_model,
        "rate_limit_hits": rate_limit_hits,
        "burns": burns,
        "sessions": sessions_out,
        "total_sessions": total_sessions,
        "main_w_usage": main_w_usage,
        "main_empty": main_empty,
        "subagent_files": subagent_files,
        "subagent_only_sessions": subagent_only_sessions,
        "total_prompts": total_prompts,
        "total_turns": total_turns,
        "ctx_traces": [
            {
                "file_key": fk,
                "session_id": sid,
                "is_main": bool(is_main),
                "model": model or "",
                "turns": [
                    {"t": i, "ctx": int(t.get("input", 0) or 0)}
                    for i, t in enumerate(turns or [])
                    if isinstance(t, dict)
                ],
            }
            for (fk, sid, is_main, model, turns) in ctx_traces_rows
        ],
        "response_sizes": [
            {
                "ts": _iso(bucket),
                "model": m,
                "n": int(n or 0),
                "p50": float(p50 or 0),
                "p90": float(p90 or 0),
            }
            for (bucket, m, n, p50, p90) in response_sizes_rows
        ],
        "ctx_lines": ctx_lines,
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
async def list_sessions(
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
async def session_detail(session_id: str) -> dict:
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
