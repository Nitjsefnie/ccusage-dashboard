"""Source-level guards for two JSX wiring mistakes that render silently.

Neither is catchable by the rest of the suite: node cannot parse JSX and
nothing here renders React, so a panel can pass every test while drawing
nothing (see test_vbar_label_geometry.py's note on that boundary). Both
of these shipped in the Cost by Context panel and were found by a human
looking at the screen, which is the failure mode these assertions close.

1. `COL` is keyed by SERIES NAME. Indexing it numerically yields
   undefined, and an SVG <rect fill={undefined}> renders BLACK while a
   <path stroke={undefined}> renders nothing -- no error, no warning.
2. `DashTooltip` takes ONE `tip` prop. Spreading the tip object instead
   leaves `tip` undefined, so its `if (!tip) return null` fires on every
   hover and the panel is silently unhoverable.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "src" / "dashboard-charts.jsx"
EXTRA = ROOT / "src" / "dashboard-charts-extra.jsx"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT a mistake is not read as the
    mistake. The lookbehind spares `https://` (a colon precedes those
    slashes), which is the only // that shows up mid-expression here.
    """
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _col_keys() -> set[str]:
    """The keys actually defined on `const COL = {...}`."""
    src = CHARTS.read_text(encoding="utf-8")
    m = re.search(r"^const COL = \{(.*?)^\};", src, re.S | re.M)
    assert m, "could not locate `const COL = {...}` in dashboard-charts.jsx"
    return set(re.findall(r"^\s*(\w+):", m.group(1), re.M))


def test_col_palette_is_never_indexed_numerically():
    """COL_X[0] is undefined -> a black bar, not an error."""
    for path in (CHARTS, EXTRA):
        hits = re.findall(
            r"\bCOL(?:_X)?\[\s*\d+\s*\]",
            _strip_line_comments(path.read_text(encoding="utf-8")))
        assert not hits, f"{path.name} indexes the COL palette numerically: {hits}"


def test_every_referenced_palette_key_exists():
    """A typo'd key fails exactly like a numeric index: undefined fill."""
    known = _col_keys()
    assert known, "COL parsed as empty - the guard would pass vacuously"
    used = set(re.findall(
        r"\bCOL(?:_X)?\.(\w+)",
        _strip_line_comments(EXTRA.read_text(encoding="utf-8"))))
    unknown = used - known
    assert not unknown, f"panels reference undefined COL keys: {sorted(unknown)}"


def test_dash_tooltip_is_passed_the_tip_prop_not_a_spread():
    """`<DashTooltip {...tip} />` leaves the `tip` prop undefined, so the
    component returns null and the panel never shows a tooltip."""
    src = EXTRA.read_text(encoding="utf-8")
    uses = re.findall(r"<window\.DashTooltip\s+([^/>]*)/>", src)
    assert uses, "no DashTooltip usages found - the guard would pass vacuously"
    bad = [u.strip() for u in uses if "tip={tip}" not in u]
    assert not bad, f"DashTooltip called without tip={{tip}}: {bad}"


def test_cumulative_line_is_anchored_to_bucket_edges():
    """A cumulative curve over bins reaches its value at the bin's UPPER
    bound, so plotting it at the bin centre states it half a bucket early
    — and leaves the line floating inside the bars instead of spanning
    them. Pin the edge anchoring: start at the plot's left edge with a
    zero, then step to each bucket's right edge.
    """
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    m = re.search(r"const cumPath = React\.useMemo\(\(\) => \{(.*?)\}, \[",
                  src, re.S)
    assert m, "could not locate the cumPath builder"
    body = m.group(1)
    assert "bw / 2" not in body, (
        "cumPath centre-anchors its points; a cumulative value belongs on "
        "the bucket's right edge")
    assert "(i + 1) * bw" in body, "cumPath must step to each bucket's right edge"
    assert re.search(r"\$\{padL\},\$\{yShare\(0\)\}", body), (
        "cumPath must start at the left edge of the first bar at zero")
