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

import math
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


def _oklch_to_srgb(L_, C, H):
    """OKLCH -> GAMMA-ENCODED sRGB, clipped.

    Gamma-encoded on purpose: SVG/CSS `opacity` composites in that space,
    so a bar drawn at 0.55 over the surface must be mixed there too.
    Compositing in linear light instead reports this panel at 2.69:1
    when the browser actually renders 4.32:1 — a colour-space slip that
    silently makes the gate wrong in the strict direction.
    """
    h = math.radians(H)
    a, b = C * math.cos(h), C * math.sin(h)
    lc = (L_ + 0.3963377774 * a + 0.2158037573 * b) ** 3
    mc = (L_ - 0.1055613458 * a - 0.0638541728 * b) ** 3
    sc = (L_ - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc,
           -1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc,
           -0.0041960863 * lc - 0.7034186147 * mc + 1.7076147010 * sc)

    def encode(c):
        c = min(1.0, max(0.0, c))
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return tuple(encode(c) for c in rgb)


def _hex_to_srgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _relative_luminance(srgb):
    """WCAG luminance: linearise HERE, after any compositing is done."""
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
           for c in srgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_cumulative_line_stays_legible_over_the_bars():
    """The line crosses the bars, so its contrast AGAINST A BAR is what
    decides whether it can be seen — and that is a number, not a taste.

    It shipped at 1.07:1: a blue line on gold bars, near-identical
    luminance, invisible wherever the two met. Hue cannot rescue that;
    only a lightness delta can. 3:1 is the floor for a non-text mark.
    """
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    cum = re.search(r"const CUM_COLOR = '(#[0-9a-fA-F]{3,6})'", src)
    opacity = re.search(r"const BAR_OPACITY = ([\d.]+)", src)
    assert cum and opacity, "could not read CUM_COLOR / BAR_OPACITY"

    css = (ROOT / "public" / "app.css").read_text(encoding="utf-8")
    gold = re.search(r"--gold:\s*oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)", css)
    surface = re.search(r"--bg-card:\s*(#[0-9a-fA-F]{6})", css)
    assert gold and surface, "could not read --gold / --bg-card from app.css"

    bar_rgb = _oklch_to_srgb(*(float(g) for g in gold.groups()))
    surf_rgb = _hex_to_srgb(surface.group(1))
    alpha = float(opacity.group(1))
    # Bars are drawn semi-transparent, so what the line actually crosses
    # is the COMPOSITE, not the raw swatch.
    composited = tuple(alpha * b + (1 - alpha) * s
                       for b, s in zip(bar_rgb, surf_rgb))
    line_rgb = _hex_to_srgb(cum.group(1))

    over_bar = _contrast(line_rgb, composited)
    bar_over_surface = _contrast(composited, surf_rgb)
    assert over_bar >= 3.0, (
        f"cumulative line vs bar is {over_bar:.2f}:1 — it will disappear "
        f"where it crosses a bar")
    # Dimming the bars to make room for the line must not push the bars
    # themselves under the same floor; both constraints hold at once.
    assert bar_over_surface >= 3.0, (
        f"bars vs surface is {bar_over_surface:.2f}:1 — bars too dim")
