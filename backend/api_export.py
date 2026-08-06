"""GET /api/export — render the full matplotlib dashboard PNG.

Split out of api.py (issue #8 module split). Runs the reference plot
script as a subprocess against the viz DB and streams back the PNG.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from starlette.responses import Response

from backend.api_common import _parse_range

router = APIRouter()

# Export-PNG render plumbing -------------------------------------------------
# System python (has matplotlib + psycopg); the app .venv does not. Override
# via EXPORT_PYTHON for dev/test boxes where matplotlib lives elsewhere.
_EXPORT_PYTHON = os.environ.get("EXPORT_PYTHON", "/usr/bin/python3")
_EXPORT_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts/plots/ccusage_plot_db.py")
_EXPORT_TIMEOUT_S = 120
_export_lock = asyncio.Semaphore(1)


def build_export_argv(rng: str, project: str | None, out_path: str) -> list[str]:
    """Construct the argv for the plot subprocess. The child inherits
    DATABASE_URL_VIZ from the environment, so the DSN is NOT passed on the
    command line (keeps credentials out of the process list)."""
    argv = [_EXPORT_PYTHON, _EXPORT_SCRIPT, "-o", out_path]
    if rng == "all":
        argv.append("--all")
    else:
        argv += ["-p", rng]
    if project:
        argv += ["--project", project]
    return argv


def _export_filename(rng: str, project: str | None) -> str:
    """Safe download filename: claudit_<project-or-all>_<range>.png."""
    proj_slug = re.sub(r"[^A-Za-z0-9._-]", "_", project) if project else "all"
    return f"claudit_{proj_slug}_{rng}.png"


async def _render_export(argv: list[str], out_path: str) -> None:
    """Run the plot subprocess, bounded by _EXPORT_TIMEOUT_S. Raises
    HTTPException(503) on timeout, HTTPException(500) on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXPORT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(503, "export render timed out") from None
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace")[-500:]
        print(f"[export] render failed (rc={proc.returncode}): {tail}", file=sys.stderr)
        raise HTTPException(500, "export render failed")


@router.get("/export")
async def export_png(
    rng: str = Query("30d", alias="range"),
    project: str | None = Query(None),
):
    """Render the full matplotlib dashboard PNG for the active filters.
    Logged-in only (guests are blocked in session.auth_middleware)."""
    _parse_range(rng)  # validation only — raises HTTPException(400) on garbage
    if _export_lock.locked():
        raise HTTPException(503, "an export is already in progress; try again shortly")
    fd, out_path = tempfile.mkstemp(suffix=".png", prefix="claudit_export_")
    os.close(fd)
    try:
        argv = build_export_argv(rng, project, out_path)
        async with _export_lock:
            await _render_export(argv, out_path)
        with open(out_path, "rb") as fh:
            png = fh.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Content-Disposition":
                f'attachment; filename="{_export_filename(rng, project)}"'
        },
    )
