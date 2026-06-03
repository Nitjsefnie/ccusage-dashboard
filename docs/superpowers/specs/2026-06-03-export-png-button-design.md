# Export PNG Button — Design

**Date:** 2026-06-03
**Status:** Approved (pending spec review)

## Summary

Add an **Export PNG** button to the ccudash dashboard that renders the full
matplotlib usage dashboard (the 8-panel figure produced by
`scripts/plots/ccusage_plot_db.py`) for the **currently-active project and
time-range filters**, and downloads it to the browser.

The plotting script already reads the deduped, cost-priced `records` table
with the same `DISTINCT ON (r.uuid)` semantics as `/api/dashboard`, so the
plot is data-consistent with the live dashboard by construction. The only
gaps today are: (a) the script has no project filter, and (b) nothing wires
it to the web app.

## Architecture

```
Browser  ──GET /api/export?range=&project=──▶  FastAPI (backend/api.py)
                                                  │ guest guard + arg map
                                                  │ asyncio.Semaphore(1)
                                                  ▼
                              asyncio.create_subprocess_exec(
                                /usr/bin/python3  scripts/plots/ccusage_plot_db.py
                                --db-url <DSN>  -o <tempfile>  [-p Nd | --all]  [--project P])
                                                  │ (matplotlib render, ≤120s)
                                                  ▼
                              read PNG bytes → unlink temp → Response(image/png, attachment)
```

**Why subprocess (not in-process):** reuses the working script verbatim,
keeps the heavy `matplotlib` dependency out of the app `.venv`, and isolates
the CPU-bound render in a separate process so it never blocks the uvicorn
event loop. Matches the "since it works" intent.

## Components

### 1. `GET /api/export` (`backend/api.py`)

- **Params:** `range: str = Query("30d")`, `project: str | None = Query(None)`
  — identical to `/api/dashboard`, so the frontend forwards its active
  filters unchanged.
- **Guest guard:** export is a **logged-in-only** feature, enforced in the
  existing auth middleware (`backend/session.py`), *not* per-endpoint. Add
  `/api/export` to the guest-blocked path check at `session.py:180-183`
  (alongside `/api/projects` and `/api/sessions*`) so a guest hitting it gets
  the same `403 Forbidden (guest)` JSON. This keeps the gate in one place and
  matches the established pattern. (The endpoint body itself needs no guest
  check.)
- **Range mapping:** `all` → `["--all"]`; otherwise `["-p", range]`
  (`1d/7d/30d/90d/365d` all satisfy the script's `(\d+)[hdwm]` period regex).
- **Invocation:** `asyncio.create_subprocess_exec` of **system
  `/usr/bin/python3`** (the interpreter that has matplotlib + psycopg; the
  app `.venv` does not) running the script with:
  - `--db-url <resolved DATABASE_URL_VIZ>` — passed **explicitly** from the
    backend's own env so the script's hard-coded `/root/session-viz/.env`
    default is never relied on (the deployed service runs from
    `/opt/ccudash` with `EnvironmentFile=/opt/ccudash/.env`).
  - `-o <tempfile>` — a `tempfile.mkstemp(suffix=".png")` path.
  - the range args, and `--project <project_id>` when set.
  - Script path resolved relative to the backend package
    (`<repo_root>/scripts/plots/ccusage_plot_db.py`).
- **Single-flight + timeout:** a module-level `asyncio.Semaphore(1)`
  serialises renders; `asyncio.wait_for(proc.communicate(), timeout=120)`
  bounds runtime (all-history ≈ 165k records). On timeout: kill the process,
  return **503**. On non-zero exit: return **500** with the captured stderr
  tail logged.
- **Response:** read the temp PNG bytes, `os.unlink` the temp file, return
  `Response(content=png_bytes, media_type="image/png",
  headers={"Content-Disposition": 'attachment;
  filename="ccusage_<project-or-all>_<range>.png"'})`.
- **No response caching** initially (explicit user action, heavy binary,
  data changes each ingest). `@cache_response` is *not* applied. YAGNI;
  revisit if export traffic ever warrants it.

### 1b. Guest gate (`backend/session.py`)

Extend the middleware guest path check (`session.py:180-183`) from

```python
if path == "/api/projects" or path.startswith("/api/sessions"):
```

to also match `path.startswith("/api/export")`. One-line change; no new
guard logic.

### 2. `--project` arg in `scripts/plots/ccusage_plot_db.py`

- Add `parser.add_argument("--project", default=None)`.
- `load_events(cutoff, end, project=None)`: when `project` is set, change the
  `FROM records r` to `FROM records r JOIN files f ON f.file_key = r.file_key`
  and add `AND f.project_id = %(project)s` to the WHERE clause, with
  `project` bound in the params dict. Mirrors `/api/dashboard`'s
  `proj_filter = "AND f.project_id = %s"`.
- `find_limit_hits()`: when a project is set, scope the `files` scan to
  `WHERE ... AND project_id = %(project)s` so the burn-rate panel's
  rate-limit verticals match the filtered view.
- No `--project` → byte-identical behaviour to today (regression-safe).

### 3. Export button (`src/app.jsx`)

- A small `Export PNG` button rendered next to `RangePicker` (per-view export
  of the active filters), gated on **`backendOn && !isGuest`** — guests never
  see it (matching the endpoint's 403).
- On click: navigate/open
  `/api/export?range=${activeRange}${activeProject ? '&project=' +
  encodeURIComponent(activeProject) : ''}`. The `attachment` response makes
  the browser download it directly — no blob/fetch plumbing.
- A short-lived `exporting` flag disables the button briefly on click so a
  double-click can't launch two 120s renders.

## Data flow

The dashboard view holds `activeRange` and `activeProject` in state (already
fed to `/api/dashboard`). The Export button reads the same two values and
forwards them to `/api/export`. The script re-derives the identical deduped
record set from Postgres, so the PNG matches the on-screen panels.

## Error handling

| Condition | Behaviour |
|---|---|
| Guest hits `/api/export` (with or without `project=`) | 403; export is logged-in-only |
| Render exceeds 120s | kill subprocess, 503 |
| Script exits non-zero | 500; stderr tail logged server-side |
| Concurrent export requests | serialised by `Semaphore(1)` (queued, not failed) |
| No records in range | script already exits non-zero with a message → 500 (acceptable; rare) |

## Testing (`tests/test_api.py`)

- Monkeypatch the subprocess runner; assert `/api/export`:
  - builds the correct argv for a `range` (`-p 7d`) and for `all` (`--all`),
  - includes `--project` when `project` is passed and omits it otherwise,
  - passes `--db-url`,
  - returns `media_type="image/png"` with an `attachment` Content-Disposition
    when the (faked) render "succeeds".
- **Guest 403** is enforced in `session.py` middleware, which the
  `test_api.py` harness bypasses (it mounts only `api.router`). So the guest
  gate is asserted separately — extend the existing middleware/guest test
  (the same one covering `/api/projects` and `/api/sessions` 403s) to include
  `/api/export`, rather than testing it through the router-only harness.
- A real matplotlib render is **not** exercised in the suite (heavy;
  matplotlib absent from the test venv). The subprocess boundary is the
  asserted seam.

## Deployment notes

- Requires `/usr/bin/python3` on the box to have `matplotlib` + `psycopg`
  (verified present) and the script to be deployed under
  `/opt/ccudash/scripts/plots/ccusage_plot_db.py` — the deploy sync must
  include `scripts/plots/`.
- `--db-url` is passed explicitly, so the script's `/root/session-viz/.env`
  default is irrelevant in production.
- No new entry in `backend/requirements.txt` (matplotlib stays out of the
  app venv by design).

## Out of scope (YAGNI)

- Exporting the `model` filter (design covers project + range only, per
  request). Trivial to add later via the same arg-mapping path.
- Async job queue / progress UI — renders are seconds; single-flight + a
  brief disabled button suffice.
- Response caching of the PNG.
