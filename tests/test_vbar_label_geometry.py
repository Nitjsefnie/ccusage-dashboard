"""Unit tests for VBar's rotated axis-label geometry (issue #9).

Cost by Project drew each project label as ONE end-anchored <text>
rotated -35deg inside an SVG whose bottom padding was fixed at 104px, so
a long name ran past the bottom-left edge and was clipped. The fix wraps
labels into <tspan> lines (never truncation) and DERIVES padB from the
deepest wrapped label through three pure helpers in
src/dashboard-charts.jsx:

  * wrapAxisLabel    — lossless hard-wrap into <= maxChars lines;
  * axisLabelDepthPx — vertical reach below the anchor of one wrapped
                       label: max over line k of
                       k*lineH*cos(A) + len_k*charPx*sin(A), because the
                       tspan dy step is rotated with the text;
  * axisLabelMaxChars — chars per line, capped so a line's horizontal
                       projection fits the bar slot and the leftmost
                       label cannot cross the SVG's left edge;
  * vbarPadB         — LABEL_Y + deepest label + one line-height of
                       descent clearance.

These tests drive the REAL helper block (extracted verbatim from the
.jsx — node cannot parse JSX, but the block is plain JS) through node,
matching the repo's no-build-step rule (same approach as
test_parser_js_mirror.py). React rendering itself is NOT exercised — the
rendered pixel result is unverified without a browser; what is pinned
here is the maths padB is computed from, plus source-level assertions
that the JSX consumes the same constants.
"""
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHARTS_JSX = ROOT / "src" / "dashboard-charts.jsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

ANGLE = 35.0
LINE_H = 12.0   # VBAR_LABEL_FS 10 * 1.2
LABEL_Y = 10.0


def _node_probe():
    script = f"""
      global.window = {{}};
      const fs = require('fs');
      const src = fs.readFileSync({str(CHARTS_JSX)!r}, 'utf8');
      const start = src.indexOf('// --- Rotated axis-label geometry');
      const end = src.indexOf('// --- Vertical bar chart');
      if (start < 0 || end < 0 || end <= start) {{
        throw new Error('geometry block extraction failed');
      }}
      eval(src.slice(start, end) +
        // const/let stay scoped to the eval; export them explicitly.
        '\\nwindow.__consts = {{ angle: VBAR_LABEL_ANGLE, fs: VBAR_LABEL_FS,' +
        ' lineH: VBAR_LABEL_LINE_H, y: VBAR_LABEL_Y }};');
      const consts = window.__consts;

      const sin = Math.sin({ANGLE} * Math.PI / 180);
      const cos = Math.cos({ANGLE} * Math.PI / 180);

      const longName = 'a-very-long-project-display-name-that-once-overflowed';
      const wrapped = wrapAxisLabel(longName, 12);

      const slotWide = 107, padL = 12, charPx = 6.4;
      const shortRows = [{{ label: 'abc' }}];
      const longRows = [{{ label: longName }}];

      console.log(JSON.stringify({{
        constants: consts,
        wrap: {{
          short: wrapAxisLabel('abc', 5),
          exact: wrapAxisLabel('abcdef', 3),
          empty: wrapAxisLabel('', 4),
          clamped: wrapAxisLabel('abcd', 0),
          wrapped: wrapped,
          lossless: wrapped.join('') === longName,
          maxLen: Math.max(...wrapped.map((l) => l.length)),
          // Separator-aware wrap (issue: breaks landing mid-token, e.g.
          // "-root-robot-isla" / "nds"): break on '/', '-', '_' and keep
          // the separator at the end of the line it closes.
          cleanBreak: wrapAxisLabel('-root-robot-islands', 16),
          multiBreak: wrapAxisLabel(
            'C--Users-multimedia-Desktop-kvalita', 16),
          hardCutFallback: wrapAxisLabel(
            'abcdefghijklmnopqrstuvwxyz0123456789', 16),
          shortNoWrap: wrapAxisLabel('kvalita', 16),
        }},
        depth: {{
          oneLine: axisLabelDepthPx(['aaaaaaaaaa'], 6, 12, {ANGLE}),
          twoLines: axisLabelDepthPx(['a', 'aaaaaaaaaa'], 6, 12, {ANGLE}),
          expectOneLine: 10 * 6 * sin,
          expectTwoLines: 12 * cos + 10 * 6 * sin,
        }},
        maxChars: {{
          wide: axisLabelMaxChars(slotWide, padL, charPx, {ANGLE}),
          expectWide: Math.floor(
            Math.min(slotWide, padL + slotWide / 2) / cos / charPx),
          tiny: axisLabelMaxChars(2, padL, charPx, {ANGLE}),
        }},
        padB: {{
          short: vbarPadB(shortRows, slotWide, padL, charPx),
          long: vbarPadB(longRows, slotWide, padL, charPx),
          none: vbarPadB([], slotWide, padL, charPx),
        }},
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module", name="js")
def _js_probe():
    return _node_probe()


def test_constants_match_the_rotation_and_font_the_jsx_uses(js):
    # If the JSX's rotate(-35 ...) or fontSize 10 drifts from these
    # constants, padB is computed for a different geometry than rendered.
    assert js["constants"] == {"angle": 35, "fs": 10, "lineH": 12, "y": 10}
    src = CHARTS_JSX.read_text()
    assert "rotate(-${VBAR_LABEL_ANGLE}" in src
    assert "fontSize={VBAR_LABEL_FS}" in src


def test_wrap_never_truncates(js):
    w = js["wrap"]
    assert w["short"] == ["abc"]           # short label: untouched
    assert w["exact"] == ["abc", "def"]    # exact multiple: no spare line
    assert w["empty"] == [""]              # empty label: one empty line
    assert w["clamped"] == ["a", "b", "c", "d"]  # maxChars < 1 clamps to 1
    assert w["lossless"]                   # every character survives
    assert w["maxLen"] <= 12


def test_wrap_breaks_on_separators_not_mid_token(js):
    # Blind every-N-chars chunking split "-root-robot-islands" into
    # "-root-robot-isla" / "nds" — mid-word. The wrap must instead break
    # on '/', '-', '_' and keep the separator at the end of the line.
    w = js["wrap"]
    assert w["cleanBreak"] == ["-root-robot-", "islands"]
    assert w["multiBreak"] == ["C--Users-", "multimedia-", "Desktop-kvalita"]
    # No separator anywhere in the 16-char window (e.g. a long hash):
    # fall back to the old hard cut at exactly maxChars.
    assert w["hardCutFallback"] == [
        "abcdefghijklmnop", "qrstuvwxyz012345", "6789"]
    # A label already <= maxChars needs no wrapping at all.
    assert w["shortNoWrap"] == ["kvalita"]


def test_depth_is_the_rotated_line_step_plus_rotated_length(js):
    d = js["depth"]
    assert d["oneLine"] == pytest.approx(d["expectOneLine"])
    # The second (longer) line sits one rotated line-step lower and wins.
    assert d["twoLines"] == pytest.approx(d["expectTwoLines"])
    assert d["twoLines"] > d["oneLine"]


def test_max_chars_bounds_horizontal_projection(js):
    mc = js["maxChars"]
    assert mc["wide"] == mc["expectWide"]
    assert mc["wide"] >= 1
    # A slot narrower than one char still yields a usable wrap width.
    assert mc["tiny"] == 1


def test_padb_shrinks_for_short_labels_and_grows_for_long_ones(js):
    p = js["padB"]
    # No empty gutter: a 3-char label needs a fraction of the old fixed
    # 104px padding.
    assert p["short"]["padB"] == pytest.approx(
        LABEL_Y + 3 * 6.4 * math.sin(math.radians(ANGLE)) + LINE_H)
    assert p["short"]["padB"] < 104 / 2
    # A long name wraps and padB grows to fit the deepest line — the
    # padding minus the fixed offsets covers the computed depth exactly.
    depth = p["long"]["padB"] - LABEL_Y - LINE_H
    lines = js["wrap"]["wrapped"]
    sin = math.sin(math.radians(ANGLE))
    cos = math.cos(math.radians(ANGLE))
    deepest = max(
        k * LINE_H * cos + len(ln) * 6.4 * sin for k, ln in enumerate(lines))
    assert depth == pytest.approx(deepest)
    assert p["long"]["padB"] > p["short"]["padB"]
    # No rows: just the anchor offset plus one line of clearance.
    assert p["none"]["padB"] == pytest.approx(LABEL_Y + LINE_H)
