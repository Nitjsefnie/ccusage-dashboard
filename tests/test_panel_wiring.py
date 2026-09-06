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


def _panel_src(name: str) -> str:
    """Just ONE component's body.

    Searching the whole file is how a guard silently checks the wrong
    panel: several of them define an `onMove`, and a bare regex takes
    whichever appears first. Slice from the component to the next
    top-level `function`/`window.` and search inside that.
    """
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    start = src.index(f"function {name}(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def test_cost_by_context_replicates_the_reference_mark_treatment():
    """Cost by Context is the same shape as TimeSeriesPanel's Cost (USD)
    -- cost bars with a cumulative line over them -- so it uses that
    panel's treatment rather than a hand-rolled one.

    The mechanism matters, not just the look. A cumulative line in a
    CONTRASTING hue has to out-lighten the bars and stay visible on the
    dark surface at the same time, and nothing does both: the first
    attempt scored 1.07:1 against the bars and was invisible. The
    reference solves it without a second colour -- a dim field (bars at
    0.3, lifted to 0.85 on hover) under a same-hue line ringed in white
    at 0.15 opacity. Pin all three; dropping the halo or brightening the
    bars quietly reintroduces the original bug.
    """
    ref = _strip_line_comments(CHARTS.read_text(encoding="utf-8"))
    src = _panel_src("CostByContextPanel")

    # The reference's own values, so this tracks it instead of freezing a
    # copy of numbers that may move.
    assert 'stroke="#fff" strokeOpacity="0.15" strokeWidth="4"' in ref, (
        "TimeSeriesPanel no longer haloes its cumulative line -- reread it "
        "before changing the panel that copies it")
    assert "fillOpacity={isHover ? 0.85 : 0.3}" in ref

    # The opacity constants sit at module scope, above the component --
    # uniquely named, so the whole file is the right place to read them.
    whole = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    rest = re.search(r"const BAR_OPACITY = ([\d.]+)", whole)
    hover = re.search(r"const BAR_OPACITY_HOVER = ([\d.]+)", whole)
    assert rest and hover, "could not read the panel's bar opacities"
    assert float(rest.group(1)) <= 0.35, (
        f"bars at {rest.group(1)} are too bright a field for a same-hue "
        f"line to read over")
    assert float(hover.group(1)) > float(rest.group(1)), (
        "hover must brighten the bar, as the reference does")

    assert 'stroke="#fff" strokeOpacity="0.15" strokeWidth="4"' in src, (
        "the cumulative line lost its white halo -- it is what makes a "
        "same-hue line legible over the bars")
    assert 'data-cumulative-line=""' in src


def test_cost_by_context_hover_matches_the_reference():
    """Hover is on the CONTAINER (the tooltip's offsetParent) and guarded
    to the plot area, so the tip does not appear over the header or the
    x-axis gutter -- both straight from TimeSeriesPanel."""
    src = _panel_src("CostByContextPanel")
    m = re.search(r"function onMove\(e\) \{(.*?)\n  \}", src, re.S)
    assert m, "could not locate the panel's hover handler"
    body = m.group(1)
    assert "ref.current.getBoundingClientRect()" in body, (
        "hover must measure against the container, not the <svg>")
    assert "my < padT || my > padT + plotH" in body, (
        "hover must be guarded to the plot area")
    assert "onMouseMove={onMove}" in src and "onMouseLeave" in src
