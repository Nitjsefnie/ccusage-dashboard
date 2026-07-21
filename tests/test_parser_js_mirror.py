"""SV-PARSER-SPEC: src/parser.js MIRRORS backend/pricing.py.

The in-browser Inspector prices transcripts with its own copy of the rate
table. If the two drift, the drag-drop view and the dashboard disagree on
cost for the same file. This asserts parity by driving the real parser.js
through node — no npm, no build step, matching the repo's no-toolchain rule.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import pricing

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"
UTC = timezone.utc

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# (model id, ISO timestamp or None)
CASES = [
    ("claude-opus-4-8", None),
    ("claude-fable-5", None),
    ("claude-fable-5[1m]", None),
    ("claude-haiku-4-5-20251001", None),
    ("claude-opus-4-20250514", None),
    ("claude-opus-4-1-20250805", None),
    ("claude-3-7-sonnet-20250219", None),
    ("claude-opus-4-9", None),
    ("claude-opus-4-8-fast", None),
    ("claude-sonnet-6", None),
    ("anthropic/claude-opus-4.8", None),
    ("us.anthropic.claude-opus-4-8", None),
    ("CLAUDE-OPUS-4-8", None),
    ("gpt-5", None),
    ("claude-sonnet-5", "2026-07-21T10:00:00Z"),
    ("claude-sonnet-5", "2026-08-31T23:59:59Z"),
    ("claude-sonnet-5", "2026-09-01T00:00:00Z"),
    ("claude-sonnet-5", None),
    ("claude-opus-4-8", "2026-07-21T10:00:00Z"),
]

_KEYMAP = {"fresh": "fresh", "c5": "create_5m", "c1h": "create_1h",
           "read": "read", "out": "output"}


def _node_rates():
    script = f"""
      global.window = {{}};
      require({str(PARSER_JS)!r});
      const cases = {json.dumps(CASES)};
      const out = cases.map(([m, ts]) => {{
        const r = window.resolveModelRate(m, ts);
        return {{ model: m, ts, kind: r.kind, rates: r.rates }};
      }});
      console.log(JSON.stringify(out));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_parser_js_rate_table_matches_backend_pricing():
    for got in _node_rates():
        ts = (
            datetime.fromisoformat(got["ts"].replace("Z", "+00:00"))
            if got["ts"] else None
        )
        want = pricing.resolve(got["model"], ts)
        label = f"{got['model']} @ {got['ts']}"
        assert got["kind"] == want.kind, f"{label}: kind"
        for js_key, py_key in _KEYMAP.items():
            assert got["rates"][js_key] == pytest.approx(want.rates[py_key]), (
                f"{label}: {py_key}"
            )


def test_parser_js_exposes_the_same_rate_epochs():
    script = f"""
      global.window = {{}};
      require({str(PARSER_JS)!r});
      console.log(JSON.stringify(window.rateEpochs));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    js_epochs = [
        datetime.fromtimestamp(ms / 1000, tz=UTC) for ms in json.loads(proc.stdout)
    ]
    assert js_epochs == pricing.RATE_EPOCHS
