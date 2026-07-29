#!/usr/bin/env python3
"""Plot Claude Code usage by reading the claudit Postgres DB.

A database-backed rewrite that shares an ancestor with ccusage_plot.py
(upstream nhz-io/ccusage-plot), with the JSONL walker swapped for a
`claudit` SELECT — uses backend/parse.py's already-deduped + cost-priced
`records` rows. DSN comes from DATABASE_URL_VIZ in the env or .env at the
repo root.

All matplotlib rendering lives in ccusage_plot_render.py (issue #8 split):
this module is the DB + CLI half and stays importable without matplotlib.
It runs both as a script (sys.path[0] is this directory, so the plain
`ccusage_plot_render` import resolves) and as an importlib-loaded module
in the test suite, which adds this directory to sys.path itself.
"""

__version__ = "1.2.0-db"

import argparse
import os
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from ccusage_plot_render import (
    parse_datetime, parse_highlight, parse_period, resolve_tz,
)
from ccusage_plot_timeline import plot_timeline

DEFAULT_DOTENV = Path("/root/session-viz/.env")
DB_URL: str | None = None  # rebound in main()


def _read_dotenv_db_url(path: Path) -> str | None:
    """Pull DATABASE_URL_VIZ out of a .env file. Returns None if missing/unreadable."""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "DATABASE_URL_VIZ":
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _resolve_db_url(cli_url: str | None) -> str:
    if cli_url:
        return cli_url
    env_url = os.environ.get("DATABASE_URL_VIZ")
    if env_url:
        return env_url
    dotenv_url = _read_dotenv_db_url(DEFAULT_DOTENV)
    if dotenv_url:
        return dotenv_url
    print(
        f"Error: DATABASE_URL_VIZ not set (checked env and {DEFAULT_DOTENV}).",
        file=sys.stderr,
    )
    sys.exit(1)


def _row_to_event(row) -> dict:
    """One records-table row → the event dict the panels consume."""
    ts, model, fresh, out, cc, cr, e5, e1h, cost = row
    unsplit = max(0, int(cc) - int(e5) - int(e1h))
    return {
        "timestamp": ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
        "model": model or "unknown",
        "inputTokens": int(fresh),
        "outputTokens": int(out),
        "cacheCreateTokens": int(cc),
        "cacheReadTokens": int(cr),
        "eph5Tokens": int(e5),
        "eph1hTokens": int(e1h),
        "unsplitCreateTokens": unsplit,
        "totalTokens": int(fresh) + int(out) + int(cc) + int(cr),
        "costUSD": float(cost),
    }


def load_events(cutoff=None, end=None, project=None):
    """Read assistant usage events from the claudit `records` table.

    The records table is post-Phase-1: per-file (file_key, request_id)
    streaming-merge already happened at ingest. Phase 2 (cross-file uuid
    dedup) is applied here via DISTINCT ON (uuid). Per-record cost is
    precomputed by backend/pricing.py, so we don't re-run estimate_cost.

    `project`, when set, restricts to records whose file belongs to that
    project_id (mirrors /api/dashboard's `AND f.project_id = %s`).
    """
    assert DB_URL is not None
    proj_filter = "AND f.project_id = %(project)s" if project else ""
    with closing(psycopg.connect(DB_URL)) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (COALESCE(r.uuid, r.file_key || ':' || r.line_num))
                r.ts, r.model,
                r.fresh_tokens, r.output_tokens,
                r.cache_creation_tokens, r.cache_read_tokens,
                r.eph5_tokens, r.eph1h_tokens,
                r.cost_usd
            FROM records r
            JOIN files f ON f.file_key = r.file_key
            WHERE r.ts IS NOT NULL
              AND (%(cutoff)s::timestamptz IS NULL OR r.ts >= %(cutoff)s)
              AND (%(end)s::timestamptz    IS NULL OR r.ts <= %(end)s)
              {proj_filter}
            ORDER BY COALESCE(r.uuid, r.file_key || ':' || r.line_num), r.ts
            """,
            {"cutoff": cutoff, "end": end, "project": project},
        )
        rows = cur.fetchall()

    events = [_row_to_event(row) for row in rows]
    events.sort(key=lambda e: e["timestamp"])
    print(f"Loaded {len(events)} records from DB.", file=sys.stderr)
    return events


def find_limit_hits(events, project=None):
    """Read rate-limit hits from `files.rate_limit_hits` (JSONB array of
    {ts, line, content}). Window bounds and 60s dedup mirror upstream.
    `project`, when set, restricts to that project's files."""
    if not events:
        return []
    assert DB_URL is not None
    start = events[0]["timestamp"]
    end = events[-1]["timestamp"]
    proj_filter = "AND project_id = %(project)s" if project else ""
    with closing(psycopg.connect(DB_URL)) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT hit
            FROM files,
                 jsonb_array_elements(rate_limit_hits) AS hit
            WHERE rate_limit_hits IS NOT NULL
              AND jsonb_array_length(rate_limit_hits) > 0
              {proj_filter}
            """,
            {"project": project},
        )
        raw = [row[0] for row in cur.fetchall()]

    hits = []
    for h in raw:
        ts_raw = h.get("ts")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < start or ts > end:
            continue
        hits.append({"ts": ts, "text": h.get("content", "")})

    hits.sort(key=lambda e: e["ts"])
    deduped = []
    for h in hits:
        if not deduped or (h["ts"] - deduped[-1]["ts"]).total_seconds() > 60:
            deduped.append(h)
    return deduped


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plot Claude Code usage from the claudit Postgres DB"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-p",
        "--period",
        default=None,
        help="Time period, e.g. 6h, 3d, 1w, 2m (default: 24h)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Plot all history",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
    )
    parser.add_argument(
        "-o", "--output", help="Output PNG path (default: ccusage_{period}.png)"
    )
    parser.add_argument(
        "--tz",
        default=None,
        help="Timezone for x-axis and date parsing, e.g. PST, EST, UTC, Asia/Tokyo",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Postgres DSN (default: DATABASE_URL_VIZ env / .env)",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Restrict to a single project_id (joins files).",
    )
    parser.add_argument(
        "--highlight",
        default=None,
        help="Highlight a daily time window, e.g. 5-11 or 5:00-11:30 (uses --tz)",
    )
    return parser


def _resolve_date_range(args, tz):
    """Resolve --from, --to, -p combinations into (start, end, label)."""
    now = datetime.now(timezone.utc)
    date_from = args.date_from
    date_to = args.date_to
    period = args.period
    has_from = date_from is not None
    has_to = date_to is not None
    has_period = period is not None

    if has_from and has_to and has_period:
        print("Error: cannot use --from, --to, and -p together.", file=sys.stderr)
        sys.exit(1)

    if has_from and has_to:
        # Explicit range
        start = parse_datetime(date_from, tz)
        end = parse_datetime(date_to, tz)
        period_label = f"{date_from}_to_{date_to}"
    elif has_from and has_period:
        # Start date + period forward
        start = parse_datetime(date_from, tz)
        end = start + parse_period(period)
        period_label = f"{date_from}+{period}"
    elif has_from:
        # From date to now
        start = parse_datetime(date_from, tz)
        end = now
        period_label = f"{date_from}_to_now"
    elif has_to and has_period:
        # Period ending at date
        end = parse_datetime(date_to, tz)
        start = end - parse_period(period)
        period_label = f"{period}_to_{date_to}"
    elif has_to:
        print("Error: --to requires either --from or -p.", file=sys.stderr)
        sys.exit(1)
    elif has_period:
        # Period back from now
        start = now - parse_period(period)
        end = now
        period_label = period
    elif args.all:
        # All history
        start = None
        end = None
        period_label = "all"
    else:
        # Default: last 24h
        start = now - timedelta(hours=24)
        end = now
        period_label = "24h"
    return start, end, period_label


def main():
    global DB_URL
    args = _build_parser().parse_args()
    DB_URL = _resolve_db_url(args.db_url)

    tz = resolve_tz(args.tz) if args.tz else None
    start, end, period_label = _resolve_date_range(args, tz)

    print(f"Reading records from claudit ({DB_URL.split('@')[-1] if '@' in DB_URL else DB_URL}) ...", file=sys.stderr)
    events = load_events(start, end, project=args.project)

    if not events:
        print(f"No API calls found for {period_label}.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(events)} API calls for {period_label}.", file=sys.stderr)

    output_path = args.output or f"ccusage_{period_label}.png"
    highlight = parse_highlight(args.highlight) if args.highlight else None

    plot_timeline(
        events, period_label, output_path, tz=tz, highlight=highlight,
        limit_hits=lambda: find_limit_hits(events, project=args.project),
    )


if __name__ == "__main__":
    main()
