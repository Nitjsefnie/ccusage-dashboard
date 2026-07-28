"""GET /api/dashboard — hourly aggregates + sessions + ctx traces.

Split out of api.py (issue #8 module split). The endpoint body is broken
into _dashboard_sources (shared SQL fragments), _dashboard_queries (the
seven panel queries) and _dashboard_build (row folding) so no single
function trips the locals/branches/statements gates. Behaviour is
unchanged from the pre-split monolith.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query
from starlette.requests import Request

from backend import db
from backend.api_common import Phases, _bucket_seconds, _iso, _parse_range
from backend.cache import cache_response

router = APIRouter()


def _rollup_source(use_rollup: bool, roll_proj: str, roll_model: str) -> str:
    """The FROM+WHERE fragment for the pure-sums panels.

    Pre-aggregated source for everything that is a pure sum/count/min/max.
    `usage_rollup` is grain (session_id, hour, model) and is rebuilt at
    ingest, so the panels read numbers that are already summed instead of
    re-aggregating ~285k records on every request.

    It is only usable when the display bucket is at least an hour wide —
    the 24h view buckets at 5 minutes, which an hourly rollup cannot
    express. For those the live subquery below is shaped with the SAME
    column names, so every query after this point is written once.
    """
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
    return f"""
        FROM {roll_from}
        WHERE {roll_since} {roll_proj} {roll_model}
    """


def _dashboard_sources(project: str | None, model: str | None,
                       since: datetime, bucket_s: int) -> dict:
    """Shared SQL fragments + their param lists for the panel queries."""
    proj_filter = ""
    model_filter = ""
    # `file_args` feeds the later per-FILE queries, which filter on
    # f.r2_last_modified + project only — never on model. Keep it free of
    # the model argument or every one of them mis-binds its placeholders.
    file_args: list[Any] = [since]
    if project:
        proj_filter = "AND f.project_id = %s"
        file_args.append(project)
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

    use_rollup = bucket_s >= 3600
    roll_proj = "AND u.project_id = %s" if project else ""
    roll_model = "AND u.model LIKE %s" if model else ""
    roll_src = _rollup_source(use_rollup, roll_proj, roll_model)
    roll_args: list[Any] = [since]
    if project:
        roll_args.append(project)
    if model:
        roll_args.append(f"%{model}%")

    return {
        "canon_src": canon_src,
        "canon_args": canon_args,
        "roll_src": roll_src,
        "roll_args": roll_args,
        "proj_filter": proj_filter,
        "file_args": file_args,
    }


def _dashboard_queries(c, ph: Phases, bucket_s: int, src: dict) -> dict:
    """Run the seven panel queries against one connection."""
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
        {src["roll_src"]}
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        src["roll_args"],
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
        {src["canon_src"]}
          AND d.text_chars > 0
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        src["canon_args"],
    ).fetchall()

    total_sessions_row = ph.execute("total_sessions", c,
        f"""
        SELECT COUNT(DISTINCT u.session_id) AS n
        {src["roll_src"]}
        """,
        src["roll_args"],
    ).fetchone()

    # Per-project range cost for the "Cost by Project" panel, folded
    # out of the same rollup source as cost_by_model (range-, project-
    # and model-filtered alike). Sorted DESC here so the top-10/Other
    # fold below is a plain slice.
    cost_by_project_rows = ph.execute("cost_by_project", c,
        f"""
        SELECT u.project_id, SUM(u.cost_usd) AS cost_usd
        {src["roll_src"]}
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        src["roll_args"],
    ).fetchall()

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
        WHERE f.r2_last_modified >= %s {src["proj_filter"]}
        """,
        list(src["file_args"]),
    ).fetchone()

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
          {src["roll_src"]}
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
        """,
        src["roll_args"],
    ).fetchall()

    # Per-FILE ctx traces — one row per main file AND per sub-agent
    # file with usage. The "Per-Session Context Growth" panel
    # treats each file as its own conversation, so a sub-agent
    # invocation surfaces under whatever model it ran on, even if
    # there's no main session file on disk.
    ctx_traces_rows = ph.execute("ctx_traces", c,
        f"""
        WITH scoped_files AS (
          SELECT f.file_key, f.session_id, f.is_main, f.ctx_turns
          FROM files f
          WHERE f.r2_last_modified >= %s {src["proj_filter"]}
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
        list(src["file_args"]),
    ).fetchall()

    rl_rows = ph.execute("rate_limit_hits", c,
        f"""
        SELECT f.session_id, hit
        FROM files f, jsonb_array_elements(f.rate_limit_hits) AS hit
        WHERE f.r2_last_modified >= %s {src["proj_filter"]}
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
        list(src["file_args"]) + [src["file_args"][0]],
    ).fetchall()

    return {
        "hourly": hourly_rows,
        "response_sizes": response_sizes_rows,
        "total_sessions_row": total_sessions_row,
        "cost_by_project": cost_by_project_rows,
        "file_counts_row": file_counts_row,
        "sessions": sessions_rows,
        "ctx_traces": ctx_traces_rows,
        "rate_limits": rl_rows,
    }


def _hourly_entry(row, seen_hours: set) -> tuple[dict, str, float]:
    """One hourly panel entry, plus its (model, cost) for the
    cost_by_model fold. `session_count` is attributed to the first
    model row of each hour only — the rows are per (hour, model), so
    summing the column across models would double-count."""
    (hour, model, input_t, output_t, c5, c1h, cr, cost, reqs, sc) = row
    hour_iso = _iso(hour)
    is_first_for_hour = hour_iso not in seen_hours
    seen_hours.add(hour_iso)
    model_name = model or "unknown"
    return {
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
    }, model_name, float(cost or 0)


def _fold_hourly(hourly_rows) -> tuple[list, list]:
    """The hourly panel plus cost_by_model, folded from the same rows
    rather than costing its own full pass over the records."""
    hourly = []
    seen_hours: set[str | None] = set()
    cost_by_model_acc: dict[str, float] = {}
    for row in hourly_rows:
        entry, model_name, cost = _hourly_entry(row, seen_hours)
        hourly.append(entry)
        cost_by_model_acc[model_name] = (
            cost_by_model_acc.get(model_name, 0.0) + cost
        )
    cost_by_model = sorted(
        ({"model": m, "cost_usd": v} for m, v in cost_by_model_acc.items() if v > 0),
        key=lambda r: r["cost_usd"],
        reverse=True,
    )
    return hourly, cost_by_model


def _fold_cost_by_project(rows) -> list:
    """Zero-cost rows are noise (the project picker drops all-time-zero
    projects too), so they're excluded. Top 10 by range cost; the tail
    collapses into ONE "Other (N projects)" row — a bar per project is
    unreadable at ~70 projects."""
    positive = [
        {"project": p or "unknown", "cost_usd": float(cost or 0)}
        for (p, cost) in rows
        if float(cost or 0) > 0
    ]
    cost_by_project = positive[:10]
    if len(positive) > 10:
        rest = positive[10:]
        cost_by_project.append({
            "project": f"Other ({len(rest)} projects)",
            "cost_usd": sum(r["cost_usd"] for r in rest),
        })
    return cost_by_project


def _turns_projection(raw_turns) -> list:
    """Project raw ctx_turns to {t, ctx} (input is total ctx-window:
    input+cc+cr)."""
    return [
        {"t": i, "ctx": int(t.get("input", 0) or 0)}
        for i, t in enumerate(raw_turns)
        if isinstance(t, dict)
    ]


def _session_entry(row, ctx_turns_by_session: dict) -> dict:
    """One sessions-panel entry, with ctx_turns folded in from the
    traces."""
    (sid, st, et, reqs, inp, out, cc, cr, cost, dom, models_used) = row
    turns_proj = _turns_projection(ctx_turns_by_session.get(sid) or [])
    # null (not 0) when ctx_turns is empty so the UI can flag the dot
    # as "ctx unknown" instead of silently falling back to a synthetic
    # duration-based size encoding (analyst spec 2026-05-07).
    ctx_at_end = turns_proj[-1]["ctx"] if turns_proj else None
    return {
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
    }


def _fold_sessions(sessions_rows, ctx_traces_rows) -> list:
    """Per-session aggregates, with ctx_turns folded in from the traces."""
    # ctx_turns used to be its own query, but it is a strict subset of
    # ctx_traces (main files only) — the same jsonb scan run twice.
    ctx_turns_by_session = {
        sid: turns
        for (_fk, sid, is_main, _mdl, turns) in ctx_traces_rows
        if is_main
    }
    return [
        _session_entry(row, ctx_turns_by_session) for row in sessions_rows
    ]


def _fold_rate_limits(rl_rows) -> list:
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
    return rate_limit_hits


def _dashboard_build(rows: dict, rng: str, project: str | None,
                     bucket_s: int) -> dict:
    """Fold the raw panel rows into the response payload."""
    hourly, cost_by_model = _fold_hourly(rows["hourly"])
    file_counts_row = rows["file_counts_row"] or (0, 0, 0, 0, 0, 0)
    total_sessions_row = rows["total_sessions_row"]
    return {
        "range": rng,
        "project": project,
        "bucket_s": bucket_s,
        "hourly": hourly,
        "cost_by_model": cost_by_model,
        "cost_by_project": _fold_cost_by_project(rows["cost_by_project"]),
        "rate_limit_hits": _fold_rate_limits(rows["rate_limits"]),
        "sessions": _fold_sessions(rows["sessions"], rows["ctx_traces"]),
        "total_sessions": int(total_sessions_row[0] or 0) if total_sessions_row else 0,
        "main_w_usage": int(file_counts_row[0] or 0),
        "main_empty": int(file_counts_row[1] or 0),
        "subagent_files": int(file_counts_row[2] or 0),
        "subagent_only_sessions": int(file_counts_row[3] or 0),
        "total_prompts": int(file_counts_row[4] or 0),
        "total_turns": int(file_counts_row[5] or 0),
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
            for (_fk, _sid, _is_main, model, turns) in rows["ctx_traces"]
        ],
        "response_sizes": [
            {
                "ts": _iso(bucket),
                "model": m,
                "n": int(n or 0),
                "p50": float(p50 or 0),
                "p90": float(p90 or 0),
            }
            for (bucket, m, n, p50, p90) in rows["response_sizes"]
        ],
    }


@router.get("/dashboard")
def dashboard_route(
    request: Request,
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
    fresh: int = Query(0),
) -> dict:
    """Route wrapper around the cached dashboard payload.

    The cached body always carries cost_by_project (one cache entry shared
    by every caller, so `cache.warm(dashboard)` keeps working and a
    guest never triggers a second compute). Guests get the same payload
    MINUS that key: per-project names/costs are exactly what the guest
    gates on /api/projects and on project= exist to withhold (see
    session.auth_middleware), and /api/dashboard is guest-accessible. The
    dict comprehension copies rather than mutating so the cached object
    stays intact for later non-guest hits."""
    payload = dashboard(rng=rng, project=project, model=model, fresh=fresh)
    if bool(getattr(request.state, "is_guest", False)):
        payload = {k: v for k, v in payload.items() if k != "cost_by_project"}
    return payload


@cache_response
def dashboard(
    rng: str = Query("30d", alias="range"),
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
    delta = _parse_range(rng)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    src = _dashboard_sources(project, model, since, bucket_s)

    ph = Phases("dashboard")
    _t_sql = time.perf_counter()
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        rows = _dashboard_queries(c, ph, bucket_s, src)
    ph.mark("sql_total", time.perf_counter() - _t_sql)

    _t_build = time.perf_counter()
    out = _dashboard_build(rows, rng, project, bucket_s)
    ph.mark("build", time.perf_counter() - _t_build)
    ph.done(
        hourly=len(out["hourly"]),
        sessions=len(out["sessions"]),
        ctx_traces=len(out["ctx_traces"]),
    )
    return out
