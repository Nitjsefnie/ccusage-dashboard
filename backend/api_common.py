"""Shared helpers for the read endpoints (issue #8 module split).

api.py grew past pylint's 1000-line module gate, so the endpoint groups
moved into backend/api_{export,dashboard,sessions,cache}.py. Everything
two or more of them need lives here so the split does not create import
cycles: the timing instrumentation, the dated-rate fold, the range and
bucket pickers, and the ISO serialiser.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from backend import pricing

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
    cases = [
        f"(CASE WHEN {ts_column} >= %s THEN 1 ELSE 0 END)"
        for _ in pricing.RATE_EPOCHS
    ]
    expr = " + ".join(["0", *cases]) if cases else "0"
    return expr, list(pricing.RATE_EPOCHS)


def epoch_ts(index: int) -> datetime | None:
    """A timestamp lying inside rate epoch ``index``, for rate lookup."""
    if not pricing.RATE_EPOCHS:
        return None
    if index <= 0:
        return pricing.RATE_EPOCHS[0] - timedelta(microseconds=1)
    return pricing.RATE_EPOCHS[min(index, len(pricing.RATE_EPOCHS)) - 1]


def _empty_model_entry(model: str) -> dict:
    return {
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
    }


def _accumulate_buckets(entry: dict, rates: dict, fresh: int, cc: int,
                        cr: int, output: int, eph5: int, eph1h: int,
                        unsplit: int) -> None:
    """Price one row's tokens into the entry's per-epoch cost buckets."""
    b = entry["_buckets"]
    b["fresh"] += fresh * rates["fresh"] / 1_000_000
    b["create_5m"] += (eph5 + unsplit) * rates["create_5m"] / 1_000_000
    b["create_1h"] += eph1h * rates["create_1h"] / 1_000_000
    b["read"] += cr * rates["read"] / 1_000_000
    b["output"] += output * rates["output"] / 1_000_000


def _accumulate_model_row(acc: dict[str, dict], row) -> None:
    """Fold one (model, rate_epoch, ...) aggregate row into `acc`."""
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
    entry = acc.setdefault(model, _empty_model_entry(model))
    entry["turns"] += int(turns or 0)
    entry["fresh"] += fresh
    entry["cache_create"] += cc
    entry["cache_read"] += cr
    entry["output"] += output
    entry["eph5"] += eph5
    entry["eph1h"] += eph1h
    entry["cost_total"] += float(cost or 0)
    _accumulate_buckets(entry, rates, fresh, cc, cr, output, eph5, eph1h, unsplit)


def fold_per_model(rows) -> list[dict]:
    """Fold (model, rate_epoch, ...) aggregate rows into one entry per model.

    Token counts and cost_total sum across epochs; cost_buckets are priced
    per epoch so they always reconcile with cost_total.
    """
    acc: dict[str, dict] = {}
    for row in rows:
        _accumulate_model_row(acc, row)

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


# Activity-heatmap timezone. Bound as a SQL parameter (never interpolated);
# Postgres tzdata makes AT TIME ZONE fully DST-aware (CET/CEST transitions).
HEATMAP_TZ = "Europe/Prague"

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


def _iso(v) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)
