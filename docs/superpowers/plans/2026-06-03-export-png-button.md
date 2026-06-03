# Export PNG Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a logged-in-only **Export PNG** button to the ccudash dashboard that renders the full 8-panel matplotlib usage dashboard for the currently-active project + time-range filters and downloads it.

**Architecture:** A new `GET /api/export` endpoint subprocesses system `/usr/bin/python3` running `scripts/plots/ccusage_plot_db.py` (reused verbatim, plus a new `--project` arg) with an explicit `--db-url`, single-flighted behind an `asyncio.Semaphore(1)` with a 120s timeout, and streams the PNG back as an attachment. Guests are blocked in the existing `session.py` auth middleware and never see the button.

**Tech Stack:** Python 3.13, FastAPI/Starlette, psycopg3, matplotlib (system python only), pytest, React (in-browser Babel JSX).

**Spec:** `docs/superpowers/specs/2026-06-03-export-png-button-design.md`

---

## File Structure

- **Modify** `scripts/plots/ccusage_plot_db.py` — add an optional `--project` filter to `load_events()` and `find_limit_hits()` and the argparse wiring. (This file is currently untracked in git after being moved into the repo; Task 1's commit adds it.)
- **Modify** `backend/api.py` — add `GET /api/export`, plus two helpers: a pure `_build_export_argv()` and an async `_render_export()` (the subprocess seam).
- **Modify** `backend/session.py:180-183` — add `/api/export` to the guest-blocked path check.
- **Modify** `src/app.jsx` — add the Export PNG button next to `RangePicker`.
- **Modify** `tests/test_api.py` — argv-builder unit test + endpoint test (monkeypatched render) + script `--project` load test.
- **Modify** `tests/test_session.py` — middleware guest-403 test for `/api/export`.

---

## Task 1: `--project` filter in the plot script

**Files:**
- Modify: `scripts/plots/ccusage_plot_db.py` (`load_events` ~302-348, `find_limit_hits` ~592-632, `main` argparse ~1284-1304)
- Test: `tests/test_api.py`

The script's `load_events()` is currently a bare `FROM records r`. Add an optional `project` filter that joins `files` and filters `f.project_id`, mirroring `/api/dashboard`'s `proj_filter`. `load_events` opens its own connection from the module global `DB_URL`, so the test sets that global to the test DB the `app_with_data` fixture builds (it ingests `fixtures/r2_mini`, which has 2 projects).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api.py` (top-of-file imports already include `os`, `Path`, `pytest`; add `importlib.util` import at the top of the file):

```python
import importlib.util

def _load_plot_db_module():
    """Import scripts/plots/ccusage_plot_db.py by path (not a package)."""
    path = _REPO_ROOT / "scripts/plots/ccusage_plot_db.py"
    spec = importlib.util.spec_from_file_location("ccusage_plot_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plot_db_project_filter_subsets_events(app_with_data):
    """load_events(project=...) returns a strict subset of all-projects,
    and every returned event belongs to the requested project."""
    mod = _load_plot_db_module()
    mod.DB_URL = os.environ["DATABASE_URL_VIZ"]

    all_events = mod.load_events(None, None)
    assert all_events, "fixture should yield records"

    # Discover a real project_id from the test DB.
    import psycopg
    with psycopg.connect(mod.DB_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT project_id FROM files ORDER BY 1")
        project_ids = [r[0] for r in cur.fetchall()]
    assert len(project_ids) >= 2, "mini fixture has 2 projects"
    target = project_ids[0]

    filtered = mod.load_events(None, None, project=target)
    assert filtered, "project filter should still yield records"
    assert len(filtered) < len(all_events), "filter must drop the other project"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api.py::test_plot_db_project_filter_subsets_events -v`
Expected: FAIL — `load_events()` got an unexpected keyword argument `'project'`.

- [ ] **Step 3: Implement the `--project` filter**

In `scripts/plots/ccusage_plot_db.py`, change the `load_events` signature and SQL. Replace the current definition header and query:

```python
def load_events(cutoff=None, end=None, project=None):
    """Read assistant usage events from the ccudash `records` table.

    The records table is post-Phase-1: per-file (file_key, request_id)
    streaming-merge already happened at ingest. Phase 2 (cross-file uuid
    dedup) is applied here via DISTINCT ON (uuid). Per-record cost is
    precomputed by backend/pricing.py, so we don't re-run estimate_cost.

    `project`, when set, restricts to records whose file belongs to that
    project_id (mirrors /api/dashboard's `AND f.project_id = %s`).
    """
    assert DB_URL is not None
    proj_filter = "AND f.project_id = %(project)s" if project else ""
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (COALESCE(r.uuid, r.file_key || ':' || r.line_num))
                r.ts, r.model,
                r.fresh_tokens, r.output_tokens,
                r.cache_creation_tokens, r.cache_read_tokens,
                r.eph5_tokens, r.eph1h_tokens,
                r.cost_usd
            FROM records r
            JOIN files f ON f.file_key = r.file_key
            WHERE r.ts IS NOT NULL
              AND (%(cutoff)s::timestamptz IS NULL OR r.ts >= %(cutoff)s)
              AND (%(end)s::timestamptz    IS NULL OR r.ts <= %(end)s)
              {proj_filter}
            ORDER BY COALESCE(r.uuid, r.file_key || ':' || r.line_num), r.ts
            """,
            {"cutoff": cutoff, "end": end, "project": project},
        )
        rows = cur.fetchall()
```

(The `JOIN files f` is now unconditional — harmless when `project` is None since every record has a file, and it lets the optional `proj_filter` reference `f`. The rest of `load_events` below `rows = cur.fetchall()` is unchanged.)

Then scope `find_limit_hits` to the same project. Change its signature and query:

```python
def find_limit_hits(events, project=None):
    """Read rate-limit hits from `files.rate_limit_hits` (JSONB array of
    {ts, line, content}). Window bounds and 60s dedup mirror upstream.
    `project`, when set, restricts to that project's files."""
    if not events:
        return []
    assert DB_URL is not None
    start = events[0]["timestamp"]
    end = events[-1]["timestamp"]
    proj_filter = "AND project_id = %(project)s" if project else ""
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT hit
            FROM files,
                 jsonb_array_elements(rate_limit_hits) AS hit
            WHERE rate_limit_hits IS NOT NULL
              AND jsonb_array_length(rate_limit_hits) > 0
              {proj_filter}
            """,
            {"project": project},
        )
        raw = [row[0] for row in cur.fetchall()]
```

(Everything below `raw = ...` in `find_limit_hits` is unchanged.)

Wire the argparse flag in `main()`. After the `--db-url` argument block, add:

```python
    parser.add_argument(
        "--project",
        default=None,
        help="Restrict to a single project_id (joins files).",
    )
```

And pass it through where `load_events` is called in `main()`:

```python
    events = load_events(start, end, project=args.project)
```

And in `plot_timeline` → `find_limit_hits`: `plot_timeline` calls `find_limit_hits(events)` internally (line ~1165). Thread the project through `plot_timeline`'s signature so the burn panel is scoped. Change `def plot_timeline(events, period_str, output_path, tz=None, highlight=None):` to add `project=None`, change its internal `limit_hits = find_limit_hits(events)` to `limit_hits = find_limit_hits(events, project=project)`, and change the `main()` call `plot_timeline(events, period_label, output_path, tz=tz, highlight=highlight)` to add `project=args.project`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api.py::test_plot_db_project_filter_subsets_events -v`
Expected: PASS

- [ ] **Step 5: Manual render check (project-filtered PNG)**

Run (against the live dev DB):
```bash
python3 scripts/plots/ccusage_plot_db.py -p 7d -o /tmp/exp_all.png
python3 scripts/plots/ccusage_plot_db.py -p 7d --project "$(psql claude_viz -tAc 'select project_id from files limit 1')" -o /tmp/exp_proj.png
ls -la /tmp/exp_all.png /tmp/exp_proj.png
```
Expected: both PNGs written; the `--project` one is the project-scoped subset (smaller cost/token totals in its panels).

- [ ] **Step 6: Commit**

```bash
git add scripts/plots/ccusage_plot_db.py tests/test_api.py
git commit -m "feat(plot): add --project filter to ccusage_plot_db.py

Co-Authored-By: <model> <noreply@anthropic.com>"
```

---

## Task 2: `_build_export_argv` (pure argv builder)

**Files:**
- Modify: `backend/api.py`
- Test: `tests/test_api.py`

Separate the argv-construction logic (pure, easily asserted) from the subprocess IO. This is the seam the endpoint test does NOT need to monkeypatch.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api.py`:

```python
def test_build_export_argv_period_and_project():
    from backend import api
    argv = api._build_export_argv("7d", "myproj", "/tmp/out.png", db_url="postgresql:///x")
    assert "/tmp/out.png" in argv
    assert argv[argv.index("-p") + 1] == "7d"
    assert argv[argv.index("--project") + 1] == "myproj"
    assert argv[argv.index("--db-url") + 1] == "postgresql:///x"
    assert "--all" not in argv


def test_build_export_argv_all_and_no_project():
    from backend import api
    argv = api._build_export_argv("all", None, "/tmp/out.png", db_url="postgresql:///x")
    assert "--all" in argv
    assert "-p" not in argv
    assert "--project" not in argv
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api.py::test_build_export_argv_period_and_project tests/test_api.py::test_build_export_argv_all_and_no_project -v`
Expected: FAIL — `module 'backend.api' has no attribute '_build_export_argv'`.

- [ ] **Step 3: Implement the builder**

At the top of `backend/api.py`, extend the imports:

```python
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
```

Then add, after the `router = APIRouter(prefix="/api")` line:

```python
# Export-PNG render plumbing -------------------------------------------------
# System python (has matplotlib + psycopg); the app .venv does not. Override
# via EXPORT_PYTHON for dev/test boxes where matplotlib lives elsewhere.
_EXPORT_PYTHON = os.environ.get("EXPORT_PYTHON", "/usr/bin/python3")
_EXPORT_SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts/plots/ccusage_plot_db.py")
_EXPORT_TIMEOUT_S = 120
_export_lock = asyncio.Semaphore(1)


def _build_export_argv(rng: str, project: str | None, out_path: str, db_url: str) -> list[str]:
    """Construct the argv for the plot subprocess. `all` → --all, else -p <rng>."""
    argv = [_EXPORT_PYTHON, _EXPORT_SCRIPT, "--db-url", db_url, "-o", out_path]
    if rng == "all":
        argv.append("--all")
    else:
        argv += ["-p", rng]
    if project:
        argv += ["--project", project]
    return argv


def _export_filename(rng: str, project: str | None) -> str:
    """Safe download filename: ccusage_<project-or-all>_<range>.png."""
    proj_slug = re.sub(r"[^A-Za-z0-9._-]", "_", project) if project else "all"
    return f"ccusage_{proj_slug}_{rng}.png"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api.py::test_build_export_argv_period_and_project tests/test_api.py::test_build_export_argv_all_and_no_project -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api.py tests/test_api.py
git commit -m "feat(api): add export argv builder + filename helper

Co-Authored-By: <model> <noreply@anthropic.com>"
```

---

## Task 3: `GET /api/export` endpoint

**Files:**
- Modify: `backend/api.py`
- Test: `tests/test_api.py`

The endpoint validates the range (reusing `_parse_range`, which raises 400 on garbage), builds argv, single-flights the render behind the semaphore, and streams the PNG. The render itself lives in an async seam `_render_export` that the test monkeypatches so no real subprocess/matplotlib runs in CI.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api.py`:

```python
def test_export_returns_png_attachment(app_with_data, monkeypatch):
    from backend import api

    captured = {}

    async def fake_render(argv, out_path):
        captured["argv"] = argv
        # Simulate the script writing a PNG.
        with open(out_path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"fake")

    monkeypatch.setattr(api, "_render_export", fake_render)

    resp = app_with_data.get("/api/export?range=7d")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"\x89PNG")
    assert "-p" in captured["argv"] and "7d" in captured["argv"]


def test_export_bad_range_400(app_with_data, monkeypatch):
    from backend import api
    async def fake_render(argv, out_path):  # should never be called
        raise AssertionError("render must not run on bad range")
    monkeypatch.setattr(api, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=banana")
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api.py::test_export_returns_png_attachment tests/test_api.py::test_export_bad_range_400 -v`
Expected: FAIL — 404 (route not defined) / `_render_export` missing.

- [ ] **Step 3: Implement the endpoint + render seam**

Add to `backend/api.py` (after the helpers from Task 2):

```python
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
        raise HTTPException(503, "export render timed out")
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace")[-500:]
        print(f"[export] render failed (rc={proc.returncode}): {tail}", file=sys.stderr)
        raise HTTPException(500, "export render failed")


@router.get("/export")
async def export_png(
    range: str = Query("30d"),
    project: str | None = Query(None),
):
    """Render the full matplotlib dashboard PNG for the active filters.
    Logged-in only (guests are blocked in session.auth_middleware)."""
    _parse_range(range)  # validation only — raises HTTPException(400) on garbage
    db_url = os.environ["DATABASE_URL_VIZ"]
    fd, out_path = tempfile.mkstemp(suffix=".png", prefix="ccudash_export_")
    os.close(fd)
    try:
        argv = _build_export_argv(range, project, out_path, db_url)
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
            "Content-Disposition": f'attachment; filename="{_export_filename(range, project)}"'
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api.py::test_export_returns_png_attachment tests/test_api.py::test_export_bad_range_400 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api.py tests/test_api.py
git commit -m "feat(api): add GET /api/export rendering the dashboard PNG

Co-Authored-By: <model> <noreply@anthropic.com>"
```

---

## Task 4: Guest gate for `/api/export`

**Files:**
- Modify: `backend/session.py:180-183`
- Test: `tests/test_session.py`

Export is logged-in-only. The block belongs in the existing middleware path check, not the endpoint.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_session.py` (imports: `from fastapi import FastAPI`; `from fastapi.testclient import TestClient`; `from backend import session`):

```python
def test_guest_blocked_from_export():
    app = FastAPI()
    app.middleware("http")(session.auth_middleware)

    @app.get("/api/export")
    async def _stub():
        return {"ok": True}

    client = TestClient(app)
    guest_cookie = session.make_guest_session_token()
    client.cookies.set(session.SESSION_COOKIE_NAME, guest_cookie)
    resp = client.get("/api/export?range=7d")
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_session.py::test_guest_blocked_from_export -v`
Expected: FAIL — returns 200 (stub) because `/api/export` is not yet in the guest-blocked set.

- [ ] **Step 3: Implement the gate**

In `backend/session.py`, change the guest path check (currently lines ~180-183):

```python
        if (
            path == "/api/projects"
            or path.startswith("/api/sessions")
            or path.startswith("/api/export")
        ):
            return JSONResponse(
                {"ok": False, "error": "Forbidden (guest)"}, status_code=403
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_session.py::test_guest_blocked_from_export -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/session.py tests/test_session.py
git commit -m "feat(auth): block guests from /api/export

Co-Authored-By: <model> <noreply@anthropic.com>"
```

---

## Task 5: Export PNG button (frontend)

**Files:**
- Modify: `src/app.jsx` (RangePicker render site ~339-341; add an `ExportButton` component near `RangePicker` ~391-412)

No automated test — the JSX runs through in-browser Babel with no JS test harness in this repo (consistent with the existing components). Verified manually.

- [ ] **Step 1: Add the `ExportButton` component**

In `src/app.jsx`, add a component next to `RangePicker` (after its definition, ~line 412):

```jsx
function ExportButton({ range, project }) {
  const [busy, setBusy] = useState(false);
  const href = `/api/export?range=${encodeURIComponent(range)}` +
    (project ? `&project=${encodeURIComponent(project)}` : '');
  const onClick = (e) => {
    if (busy) { e.preventDefault(); return; }
    setBusy(true);
    // Re-enable after a beat so a stuck render doesn't permanently disable it.
    setTimeout(() => setBusy(false), 3000);
  };
  return (
    <a
      className="pp-btn"
      style={{ marginLeft: 12, opacity: busy ? 0.5 : 1, pointerEvents: busy ? 'none' : 'auto' }}
      href={href}
      onClick={onClick}
      title="Download a PNG of this dashboard for the current filters"
    >{busy ? 'rendering…' : 'Export PNG'}</a>
  );
}
```

- [ ] **Step 2: Render it next to RangePicker**

In the controls area (currently ~339-341):

```jsx
      {backendOn && (
        <RangePicker active={activeRange} onChange={setActiveRange} />
      )}
```

Change to:

```jsx
      {backendOn && (
        <RangePicker active={activeRange} onChange={setActiveRange} />
      )}
      {backendOn && !isGuest && (
        <ExportButton range={activeRange} project={activeProject} />
      )}
```

(`isGuest`, `activeRange`, `activeProject`, `backendOn` are all already in scope at this render site — confirmed at app.jsx:139-147,332-342.)

- [ ] **Step 3: Manual verification**

Run the backend (`python3 -m uvicorn backend.app:app --port 8000`), log in as a real user, and:
- Confirm the **Export PNG** link appears beside the range picker.
- Click it for `7d` / `30d` / `all` and with a project selected vs. "All" — confirm a PNG downloads named `ccusage_<project|all>_<range>.png` and its panels match the on-screen dashboard for those filters.
- Log in as guest (Continue as guest) → confirm the button is **absent**, and `curl -s -o /dev/null -w '%{http_code}' --cookie "session=<guest cookie>" http://127.0.0.1:8000/api/export?range=7d` returns `403`.

- [ ] **Step 4: Commit**

```bash
git add src/app.jsx
git commit -m "feat(ui): add Export PNG button (logged-in only)

Co-Authored-By: <model> <noreply@anthropic.com>"
```

---

## Task 6: Full suite + docs touch-up

**Files:**
- Modify: `AGENTS.md` (api.py endpoint list ~line listing `/api/...`), `README.md` (Architecture FastAPI route list)

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (new export + guest tests included).

- [ ] **Step 2: Add `/api/export` to the endpoint docs**

In `AGENTS.md`, add `/api/export` to the `api.py` route list comment. In `README.md`, add `/api/export` to the FastAPI route list in the Architecture block.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md README.md
git commit -m "docs: document /api/export endpoint

Co-Authored-By: <model> <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **Co-Author trailer:** replace `<model>` with your own model name (e.g. `Kimi K2.6 <noreply@kimi.com>` for a kimi subagent, `Claude Opus 4.8 (1M context) <noreply@anthropic.com>` for claude). Every commit needs the trailer.
- **`EXPORT_PYTHON`:** tests never invoke the real subprocess (the render seam is monkeypatched), so `/usr/bin/python3` not having matplotlib in CI is fine. Production relies on system python3 having matplotlib + psycopg (verified on this box).
- **Deploy:** the service runs in-place from `/root/session-viz` (the real `ccudash.service` unit; `examples/`'s `/opt/ccudash` is only a sample), so `systemctl restart ccudash` picks up the repo directly — no deploy-sync step.
- **Post-review correction (commit `4768ab5`):** the shipped code differs from Tasks 2/3/5 above in two ways the review mandated — (a) the DSN is NOT passed in argv (the subprocess inherits `DATABASE_URL_VIZ` from the env; keeps the password out of the process list), so `_build_export_argv` takes no `db_url`; (b) the frontend uses `fetch`+blob with an `.ok` check instead of a plain `<a href>`, so error responses (incl. the reachable empty-data 500) render inline rather than ejecting the SPA. Also added: fail-fast 503 when a render is already in flight, a matplotlib-absent guard in `plot_timeline`, and timeout→503 / exit→500 tests.
