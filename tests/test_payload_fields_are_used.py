"""Every field the API sends must be read by the frontend.

The dashboard payload had grown three dead limbs that nothing rendered:
`ctx_lines` (140 KB per response plus a 271ms query), `burns` (24 KB),
and a `file_key` on all 8,892 ctx_traces entries that was assigned to an
`id` no component ever read. Together with `sessions[].turns` — which
duplicated data already in `ctx_traces` — that was most of a 3.2 MB
response.

Nobody notices this by reading code: a field goes unused when its
consumer is deleted or rewritten, and the producer keeps paying for it in
query time, transfer and browser parse. So assert it instead.

The check is deliberately a text search, not real JS analysis. The
frontend is untyped in-browser JSX with no build step, so there is no
import graph to walk; a name appearing anywhere in `src/` is treated as
used. That direction of error is the safe one — this test under-reports
(a field referenced only in a comment passes) and will not fail on a
field that is genuinely read. It exists to catch the blatant case: a
field no file mentions at all.

To keep a field the UI does not read (an external consumer, a debugging
aid), add it to ALLOWED_UNUSED with the reason.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Reuses the API suite's fresh-DB + mini-R2 fixture rather than standing
# up a second one; having the function object in this module's globals is
# what registers the fixture here. Imported under its private function
# name — the fixture's registered name stays "app_with_data" — and listed
# in __all__ so the re-export is explicit, not an unused import.
from test_api import _app_with_data_fixture

__all__ = ["_app_with_data_fixture"]

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Fields intentionally sent without a frontend reader.
ALLOWED_UNUSED: dict[str, str] = {
    # Echoed back so a cached/again-fetched response is self-describing;
    # the client already knows what it asked for.
    "range": "echo of the request parameter",
    "project": "echo of the request parameter",
    "tz": "echo of the server-side timezone used for bucketing",
}

# Generic names that appear all over the frontend for unrelated reasons,
# so a hit proves nothing. Checking them would be security theatre.
TOO_GENERIC = {
    "n", "t", "x", "y", "id", "ts", "key", "name", "type", "value",
    "count", "total", "model", "hour", "data", "rows", "cells",
}


def _frontend_files() -> list[tuple[str, list[str]]]:
    out = []
    for pat in ("src/*.js", "src/*.jsx", "src/**/*.jsx", "public/*.html"):
        for p in sorted(_REPO_ROOT.glob(pat)):
            out.append((p.name, p.read_text(encoding="utf-8", errors="replace").splitlines()))
    assert out, "no frontend sources found — path glob is wrong"
    return out


def _match_lines(field: str, files) -> list[tuple[str, int]]:
    """Every (file, line_no) where `field` is read as a property or key."""
    f = re.escape(field)
    pat = re.compile(rf"(\.{f}\b|\[['\"]{f}['\"]\]|['\"]{f}['\"])")
    return [(name, i + 1)
            for name, lines in files
            for i, ln in enumerate(lines) if pat.search(ln)]


# How far a nested field's usage may sit from any mention of the payload
# key it arrives under before we stop believing they are related.
PROXIMITY_LINES = 120

# Full paths whose consumer receives the payload under a different, too
# generic name, so proximity to the original key cannot be established.
# Keyed by full path, so the same leaf name is still checked elsewhere.
ORPHAN_OK = {
    # ResponseSizesPanel takes it as `data={responseSizes}`; anchoring on
    # `data` would match most of the file and check nothing.
    "response_sizes[].p50": "read in ResponseSizesPanel via a `data` prop",
    "response_sizes[].p90": "read in ResponseSizesPanel via a `data` prop",
}


def _ident_lines(name: str, files) -> list[tuple[str, int]]:
    """Lines mentioning `name` as a bare identifier.

    Anchors must match this way, not as property access: the frontend
    receives payload keys as destructured props (`function ProjectPicker
    ({ projects })`), which a `.projects` pattern would miss — flagging
    every genuinely-used field on that endpoint.
    """
    pat = re.compile(rf"\b{re.escape(name)}\b")
    return [(fn, i + 1)
            for fn, lines in files
            for i, ln in enumerate(lines) if pat.search(ln)]


def _near_any(hits, anchors, window=PROXIMITY_LINES) -> bool:
    return any(fn == an and abs(ln - al) <= window
               for fn, ln in hits for an, al in anchors)


def _camel(snake: str) -> str:
    """ctx_traces -> ctxTraces; the frontend renames on the way in."""
    head, *rest = snake.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def _collect_fields(node, prefix: str, out: dict[str, str]) -> None:
    """Record every object key in the payload as prefix.key -> key."""
    if isinstance(node, dict):
        for k, v in node.items():
            out[f"{prefix}.{k}" if prefix else k] = k
            _collect_fields(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(node, list):
        # One sample element is enough: rows in a list share a shape.
        if node:
            _collect_fields(node[0], f"{prefix}[]", out)


# (path, description) for every read endpoint reachable by the dashboard.
ENDPOINTS = [
    ("/api/dashboard?range=all", "dashboard"),
    ("/api/cache?range=all", "cache view"),
    ("/api/tool-usage?range=all", "tool usage"),
    ("/api/tool-error-rate?range=all", "tool error rate"),
    ("/api/reply-latency?range=all", "reply latency"),
    ("/api/activity-heatmap?range=all", "activity heatmap"),
    ("/api/context-growth/agg?range=all", "context growth agg"),
    ("/api/models", "models"),
    ("/api/projects", "projects"),
]


@pytest.mark.parametrize("path,label", ENDPOINTS, ids=[e[1] for e in ENDPOINTS])
def test_no_unused_response_fields(app_with_data, path, label):
    r = app_with_data.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    body = r.json()

    fields: dict[str, str] = {}
    _collect_fields(body, "", fields)
    files = _frontend_files()

    dead, orphaned = [], []
    for full, leaf in sorted(fields.items()):
        if leaf in TOO_GENERIC or leaf in ALLOWED_UNUSED:
            continue
        hits = _match_lines(leaf, files)
        if not hits:
            dead.append(full)
            continue
        # Nested field: its usage should sit near a mention of the
        # top-level key it arrives under. A match on the far side of a
        # 2000-line file is a different thing that happens to share a
        # name — which is how ctx_traces[].file_key stayed alive, matching
        # only an unrelated reply-latency tooltip.
        if "[]." in full or full.count(".") >= 1:
            root = full.split("[")[0].split(".")[0]
            anchors = _ident_lines(root, files) + _ident_lines(_camel(root), files)
            if full in ORPHAN_OK:
                continue
            if anchors and not _near_any(hits, anchors):
                orphaned.append(
                    f"{full}: {leaf} appears at "
                    + ", ".join(f"{f}:{l}" for f, l in hits[:3])
                    + f" but never within {PROXIMITY_LINES} lines of `{root}`"
                )

    assert not dead, (
        f"{label} ({path}) sends {len(dead)} field(s) no frontend file "
        f"references:\n  " + "\n  ".join(dead) + "\n\n"
        "Either stop sending them, or add them to ALLOWED_UNUSED in "
        f"{Path(__file__).name} with the reason."
    )
    assert not orphaned, (
        f"{label} ({path}) sends {len(orphaned)} field(s) whose only "
        "matches look like unrelated code that shares the name:\n  "
        + "\n  ".join(orphaned) + "\n\n"
        "Confirm the field is really read off this payload. If it is, "
        f"widen PROXIMITY_LINES or allowlist it in {Path(__file__).name}."
    )
