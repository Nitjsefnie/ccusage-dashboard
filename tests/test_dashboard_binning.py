"""Frontend bin widths must not undershoot backend aggregation."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


BINNING_JS = (
    Path(__file__).resolve().parents[1] / "src" / "dashboard-binning.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def test_dashboard_bins_never_undershoot_server_aggregation():
    script = f"""
      global.window = {{}};
      require({str(BINNING_JS)!r});
      const sixDays = {{ start: 0, end: 6 * 86400000 }};
      const twoHours = {{ start: 0, end: 2 * 3600000 }};
      console.log(JSON.stringify({{
        adaptive6d: window.pickAdaptiveBinMs(sixDays.end),
        sevenDay: window.dashboardBinMs(sixDays, 3600),
        thirtyDay: window.dashboardBinMs(sixDays, 21600),
        ttlThirtyDay: window.cacheTtlBinMs(sixDays, 21600000),
        short24h: window.dashboardBinMs(twoHours, 300),
        synthetic: window.dashboardBinMs(sixDays),
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result == {
        "adaptive6d": 3_600_000,
        "sevenDay": 3_600_000,
        "thirtyDay": 21_600_000,
        "ttlThirtyDay": 21_600_000,
        "short24h": 300_000,
        "synthetic": 3_600_000,
    }
