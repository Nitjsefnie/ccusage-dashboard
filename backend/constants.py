"""Shared constants that would otherwise create import cycles.

Kept dependency-free: any module may import this one, and it imports no
other backend module.
"""
from __future__ import annotations

# Display bucket widths /api/reply-latency can ask for, from
# api._bucket_seconds. 300 (the 24h view) is deliberately absent: a row
# per 5 minutes of all history to serve one day is not worth it, and that
# range stays on the live path.
LATENCY_BUCKETS = (3600, 21600, 43200, 86400)
