"""Shared constants that would otherwise create import cycles.

Kept dependency-free: any module may import this one, and it imports no
other backend module.
"""
from __future__ import annotations

from pathlib import Path

# Display bucket widths /api/reply-latency can ask for, from
# api._bucket_seconds. 300 (the 24h view) is deliberately absent: a row
# per 5 minutes of all history to serve one day is not worth it, and that
# range stays on the live path.
LATENCY_BUCKETS = (3600, 21600, 43200, 86400)

# Context-size buckets for `ctx_cost_rollup` / the Cost by Context panel.
# The per-call window is fresh + cache_creation + cache_read, bucketed to
# CTX_BUCKET_WIDTH; everything at or above CTX_BUCKET_MAX folds into one
# open-ended overflow bucket keyed by CTX_BUCKET_MAX itself.
#
# Fixed width rather than logarithmic, and that is a measurement not a
# taste: over the live corpus, cost is near-flat across the window --
# 7.8% of all spend in 100-150k, still 5.2% in 700-750k, ~60% above
# 300k. Log buckets would compress precisely the region carrying the
# money into a handful of fat bars.
#
# These edges are BAKED INTO STORED ROWS, exactly like LATENCY_BUCKETS:
# a read cannot re-bucket to a different width, so changing either
# constant requires rebuilding the rollup (bump nothing else -- the
# rollup derives from stored `records` columns, so no reparse).
CTX_BUCKET_WIDTH = 50_000
CTX_BUCKET_MAX = 1_000_000


def ctx_bucket(ctx_tokens: int) -> int:
    """Lower edge of the bucket `ctx_tokens` falls in.

    Mirrored by the SQL in ingest.rebuild_ctx_cost_rollup(); the two must
    agree, and test_ctx_cost_rollup.py pins this side of it.
    """
    if ctx_tokens >= CTX_BUCKET_MAX:
        return CTX_BUCKET_MAX
    if ctx_tokens <= 0:
        return 0
    return (ctx_tokens // CTX_BUCKET_WIDTH) * CTX_BUCKET_WIDTH


def _read_version() -> str:
    """Repo version from the root VERSION file, or "unknown".

    The file is the single source of truth: `.github/workflows/release.yml`
    tags a release when it changes, and `speed.yml` compares against the
    release it names. Read once at import — it cannot change under a
    running process without a redeploy.

    A deploy that omits the file (a tarball, a partial checkout) gets
    "unknown" rather than a crash: /health reporting an unknown version is
    strictly better than /health not answering at all.
    """
    try:
        text = (Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8"
        )
    except OSError:
        return "unknown"
    return text.strip() or "unknown"


VERSION = _read_version()
