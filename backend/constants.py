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
