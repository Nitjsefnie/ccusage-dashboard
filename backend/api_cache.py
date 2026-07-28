"""GET /api/cache — the parse_session.py --cache replica.

Split out of api.py (issue #8 module split). The endpoint body is broken
into _cache_canon_source (shared WHERE fragment), _cache_queries (the
four queries) and _session_total (the cross-model fold) so no single
function trips the locals gate. Behaviour is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from backend import db
from backend.api_common import Phases, _parse_range, fold_per_model, rate_epoch_sql
from backend.cache import cache_response

router = APIRouter()


def _cache_canon_source(project: str | None, model: str | None,
                        since: datetime) -> tuple[str, list]:
    """The shared FROM/WHERE fragment for the four cache queries.

    Dedup used to be a CTE prefixed onto each query, so Postgres re-ran
    the whole DISTINCT ON — the single most expensive step — FOUR times
    per request (measured: 19s at range=all). It is now resolved at
    ingest into records.is_canonical (ingest.recompute_canonical), so
    each query just filters a boolean.
    """
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
    canon_src = f"""
        FROM records
        WHERE ts >= %s AND is_canonical {proj_filter} {model_filter}
    """
    return canon_src, canon_args


def _cache_queries(c, ph: Phases, canon_src: str, canon_args: list) -> tuple:
    """per_model + the three top-10 queries against one connection."""
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

    return per_model_rows, top_output, top_create, top_read


def _session_total(per_model: list) -> dict:
    """The per_model shape, summed across models."""
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
    return session_total


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


@router.get("/cache")
@cache_response
def cache_view(
    rng: str = Query("30d", alias="range"),
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
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    canon_src, canon_args = _cache_canon_source(project, model, since)

    ph = Phases("cache_view")
    with db.viz_conn() as c:
        per_model_rows, top_output, top_create, top_read = _cache_queries(
            c, ph, canon_src, canon_args
        )

    per_model = fold_per_model(per_model_rows)
    ph.done(models=len(per_model))

    return {
        "range": rng,
        "project": project,
        "per_model": per_model,
        "session_total": _session_total(per_model),
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
