# Active Issues Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve open issues #12–#16 with regression coverage, CI-clean commits, and one final push to the default branch; leave owner-deferred #10 open.

**Architecture:** Keep each fix within its owning subsystem: parser ordering, SSE lifecycle, cookie issuance, frontend bin selection, and ingest-time churn aggregation. Extract only the two contracts whose duplication caused defects—cookie attributes and bin-width selection—and preserve live-query fallbacks below rollup grain.

**Tech Stack:** Python 3.13, FastAPI/Starlette, asyncio, PostgreSQL 16+, psycopg3, React 18 classic JSX, plain JavaScript, pytest, Node, pyright, pylint, pycodestyle, ESLint.

## Global Constraints

- Work directly on the repository's default `master` branch; do not create a worktree or feature branch.
- Use fixture-driven parser tests and keep each `fixtures/parser/*.jsonl` file below 1 KB.
- Never edit `~/.claude/scripts/parse_session.py`; it is read-only canonical analyst code.
- Bump tracked `.env` `PARSER_VERSION` from 19 to 20 for the parser semantic change.
- Use parameterized SQL for values and preserve the `bucket_s >= 3600` rollup boundary.
- New unrelated reproducible bugs get remote issues with evidence, not implementation in this batch.
- Every commit carries `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`.
- Every resolving implementation commit carries `Closes #<number>`; push all local commits once after full verification.

---

### Task 1: Mixed timestamp parser ordering (#12)

**Files:**
- Create: `fixtures/parser/mixed_timestamps.jsonl`
- Modify: `tests/test_parse.py`
- Modify: `backend/parse.py`
- Modify: `.env`

**Interfaces:**
- Consumes: `parse.parse_file(file_key: str, blob: bytes) -> dict`
- Produces: `_build_ctx_turns(records: list, user_text_lines: list) -> list` with undated records ordered before dated records without naive/aware comparisons.

- [ ] **Step 1: Add the sub-1 KB failing fixture**

```jsonl
{"type":"assistant","uuid":"a0","requestId":"r0","message":{"role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"text","text":"undated"}],"usage":{"input_tokens":10,"output_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
{"type":"user","timestamp":"2026-05-07T10:00:01Z","uuid":"u1","message":{"role":"user","content":"next"}}
{"type":"assistant","timestamp":"2026-05-07T10:00:02Z","uuid":"a1","requestId":"r1","message":{"role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"text","text":"dated"}],"usage":{"input_tokens":20,"output_tokens":2,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
```

- [ ] **Step 2: Add the regression test**

```python
def test_mixed_dated_and_undated_records_sort_undated_first():
    out = parse.parse_file(
        "k/sess-mixed/sess-mixed.jsonl", _read("mixed_timestamps.jsonl")
    )
    assert [(t["ts"], t["input"]) for t in out["ctx_turns"]] == [
        ("", 10),
        ("2026-05-07T10:00:02+00:00", 20),
    ]
```

- [ ] **Step 3: Run the test and observe RED**

Run: `.venv/bin/python -m pytest tests/test_parse.py::test_mixed_dated_and_undated_records_sort_undated_first -q`

Expected: FAIL with `TypeError: can't compare offset-naive and offset-aware datetimes`.

- [ ] **Step 4: Implement explicit missing-first partitioning**

```python
aware_min = datetime.min.replace(tzinfo=timezone.utc)
sorted_recs = sorted(
    records,
    key=lambda r: (
        r["ts"] is not None,
        r["ts"] if r["ts"] is not None else aware_min,
        r["line_num"],
    ),
)
```

Import `timezone` from `datetime`, and change `.env` to `PARSER_VERSION=20`.

- [ ] **Step 5: Run focused parser verification**

Run: `.venv/bin/python -m pytest tests/test_parse.py -q`

Expected: all parser tests pass and `wc -c fixtures/parser/mixed_timestamps.jsonl` reports less than 1024 bytes.

- [ ] **Step 6: Commit the fix**

```bash
git add .env backend/parse.py fixtures/parser/mixed_timestamps.jsonl tests/test_parse.py
git commit -m "fix(parse): handle mixed missing timestamps" \
  -m "Closes #12" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 2: SSE lifecycle teardown (#13)

**Files:**
- Create: `tests/test_events.py`
- Modify: `backend/events.py`
- Modify: `backend/app.py`

**Interfaces:**
- Consumes: `events.set_loop(loop: asyncio.AbstractEventLoop) -> None`, FastAPI `lifespan`.
- Produces: `events.clear_loop() -> None`; `broadcast_threadsafe(event: str, data: dict) -> None` safely drops broadcasts after teardown.

- [ ] **Step 1: Add closed-loop and lifecycle-order regressions**

```python
import asyncio

from fastapi import FastAPI
import pytest

import backend.app as app_mod
from backend import events


def test_broadcast_after_loop_closed_is_a_noop():
    loop = asyncio.new_event_loop()
    events.set_loop(loop)
    loop.close()
    try:
        events.broadcast_threadsafe("late", {"ok": True})
    finally:
        events.clear_loop()


@pytest.mark.asyncio
async def test_lifespan_signals_before_clearing_loop(monkeypatch):
    calls = []

    class Scheduler:
        def __init__(self, **_kwargs):
            pass
        def add_job(self, *_args, **_kwargs):
            pass
        def start(self):
            pass
        def shutdown(self, *, wait):
            calls.append(("scheduler", wait))

    monkeypatch.setattr(app_mod.db, "schema_check", lambda: None)
    monkeypatch.setattr(app_mod, "BackgroundScheduler", Scheduler)
    monkeypatch.setattr(app_mod.events, "set_loop", lambda _loop: calls.append("set"))
    monkeypatch.setattr(app_mod.events, "signal_shutdown", lambda: calls.append("signal"))
    monkeypatch.setattr(app_mod.events, "clear_loop", lambda: calls.append("clear"))

    async with app_mod.lifespan(FastAPI()):
        pass

    assert calls[-3:] == ["signal", ("scheduler", False), "clear"]
```

- [ ] **Step 2: Run both tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_events.py -q`

Expected: FAIL because a closed loop raises and `events.clear_loop` does not exist.

- [ ] **Step 3: Implement lifecycle clearing and race-safe broadcasting**

```python
def clear_loop() -> None:
    global _main_loop, _shutdown_event
    _main_loop = None
    _shutdown_event = None


def broadcast_threadsafe(event: str, data: dict) -> None:
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    # build payload and target snapshot as before
    try:
        loop.call_soon_threadsafe(_put_all)
    except RuntimeError:
        if not loop.is_closed():
            raise
```

Call `events.clear_loop()` immediately after `sched.shutdown(wait=False)` in `backend/app.py`.

- [ ] **Step 4: Run focused lifecycle verification**

Run: `.venv/bin/python -m pytest tests/test_events.py tests/test_session.py tests/test_login.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the fix**

```bash
git add backend/app.py backend/events.py tests/test_events.py
git commit -m "fix(events): release loop state on shutdown" \
  -m "Closes #13" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 3: Central session-cookie contract (#14)

**Files:**
- Modify: `tests/test_session.py`
- Modify: `tests/test_login.py`
- Modify: `backend/session.py`
- Modify: `backend/login.py`

**Interfaces:**
- Consumes: Starlette `Response`, session token strings, `COOKIE_SECURE`.
- Produces: `set_session_cookie(response: Response, token: str) -> None` as the only cookie-issuance path.

- [ ] **Step 1: Add a failing helper contract test**

```python
def test_set_session_cookie_owns_complete_flag_contract(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "1")
    response = Response()
    session.set_session_cookie(response, "token")
    header = response.headers["set-cookie"].lower()
    assert "session=token" in header
    assert "httponly" in header
    assert "secure" in header
    assert "samesite=strict" in header
    assert f"max-age={session.SESSION_COOKIE_MAX_AGE}" in header
    assert "path=/" in header
    assert "domain=" not in header
```

Strengthen the route tests through their real response headers rather than
asserting on a mocked helper:

```python
def _assert_session_cookie_contract(response):
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=strict" in header
    assert f"max-age={session_mod.SESSION_COOKIE_MAX_AGE}" in header
    assert "path=/" in header
    assert "domain=" not in header


def test_authenticated_login_uses_cookie_contract(app, fake_user):
    response = TestClient(app).post(
        "/login",
        data={"user_id": "12345", "password": "hunter2"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    _assert_session_cookie_contract(response)


def test_guest_login_uses_cookie_contract(app):
    response = TestClient(app).post("/login/guest", follow_redirects=False)
    assert response.status_code == 303
    _assert_session_cookie_contract(response)
```

- [ ] **Step 2: Run the tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_session.py::test_set_session_cookie_owns_complete_flag_contract tests/test_login.py -q`

Expected: FAIL because `set_session_cookie` is absent. The route
characterization tests pass before and after the refactor, proving the
consumer-visible contract does not change while ownership moves.

- [ ] **Step 3: Implement and adopt the helper**

```python
def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="strict",
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
    )
```

Replace both duplicated blocks in `backend/login.py` with
`session_mod.set_session_cookie(response, token)` and remove the now-unused
`os` import from that module.

- [ ] **Step 4: Run auth verification**

Run: `.venv/bin/python -m pytest tests/test_session.py tests/test_login.py tests/test_auth.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the fix**

```bash
git add backend/login.py backend/session.py tests/test_login.py tests/test_session.py
git commit -m "refactor(auth): centralize session cookie flags" \
  -m "Closes #14" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 4: Server-aware frontend bins (#15)

**Files:**
- Create: `src/dashboard-binning.js`
- Create: `tests/test_dashboard_binning.py`
- Modify: `public/index.html`
- Modify: `src/app.jsx`
- Modify: `src/dashboard-charts-extra.jsx`

**Interfaces:**
- Produces: `window.pickAdaptiveBinMs(spanMs) -> number`, `window.dashboardBinMs(range, bucketS) -> number`, `window.cacheTtlBinMs(range, dashboardMs) -> number`.
- Consumes: backend `bucket_s`, frontend `{start, end}` millisecond ranges.

- [ ] **Step 1: Add a Node-driven failing regression**

Create `tests/test_dashboard_binning.py` with the complete Node probe:

```python
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
```

- [ ] **Step 2: Run the test and observe RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard_binning.py -q`

Expected: FAIL because `src/dashboard-binning.js` does not exist.

- [ ] **Step 3: Add the pure classic-script helper**

```javascript
const DASHBOARD_NICE_BINS_MS = [
  60_000, 5 * 60_000, 15 * 60_000, 30 * 60_000,
  60 * 60_000, 6 * 60 * 60_000, 12 * 60 * 60_000, 24 * 60 * 60_000,
];

function pickAdaptiveBinMs(spanMs) {
  const span = Math.max(1, Number(spanMs) || 1);
  let chosen = DASHBOARD_NICE_BINS_MS[0];
  for (const width of DASHBOARD_NICE_BINS_MS) {
    if (span / width < 100) break;
    chosen = width;
  }
  return chosen;
}

function dashboardBinMs(range, bucketS) {
  const adaptive = pickAdaptiveBinMs(range.end - range.start);
  const serverMs = Number(bucketS) * 1000;
  return Number.isFinite(serverMs) && serverMs > 0
    ? Math.max(adaptive, serverMs) : adaptive;
}

function cacheTtlBinMs(range, dashboardMs) {
  const adaptive = pickAdaptiveBinMs(range.end - range.start);
  const floorMs = Number(dashboardMs);
  return Number.isFinite(floorMs) && floorMs > 0
    ? Math.max(adaptive, floorMs) : adaptive;
}

Object.assign(window, { pickAdaptiveBinMs, dashboardBinMs, cacheTtlBinMs });
```

- [ ] **Step 4: Wire both panels to the helper**

Load `/src/dashboard-binning.js` after `synthetic-data.js` and before JSX
scripts. Replace the picker loop in `Dashboard` with:

```javascript
const binMs = window.dashboardBinMs(range, bucketS);
```

Replace Cache TTL's duplicated picker with:

```javascript
const adaptiveBin = React.useMemo(
  () => window.cacheTtlBinMs(dataRange, binMs),
  [dataRange.start, dataRange.end, binMs]
);
```

- [ ] **Step 5: Run frontend-focused verification**

Run: `.venv/bin/python -m pytest tests/test_dashboard_binning.py tests/test_vbar_label_geometry.py -q`

Run: `npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'`

Expected: tests and ESLint pass.

- [ ] **Step 6: Commit the fix**

```bash
git add public/index.html src/app.jsx src/dashboard-binning.js src/dashboard-charts-extra.jsx tests/test_dashboard_binning.py
git commit -m "fix(charts): respect backend aggregation width" \
  -m "Closes #15" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 5: Roll up line churn (#16)

**Files:**
- Modify: `backend/schema.sql`
- Modify: `backend/ingest.py`
- Modify: `backend/api_dashboard.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: hourly `tool_rollup`, live `tool_uses`, `bucket_s`, project/model filters.
- Produces: `tool_rollup.lines_added`, `tool_rollup.lines_deleted`; `_dashboard_sources(...)["churn_ts"]`; identical dashboard churn payloads from live and rolled sources.

- [ ] **Step 1: Add behavioral source-selection and rollup regressions**

Add a regression that makes the live and rolled populations deliberately
different, then observes each through `/api/dashboard`:

```python
def test_dashboard_churn_uses_rollup_only_at_hourly_grain(
    app_with_fresh_data,
):
    file_key = "projA/sess-A/sess-A.jsonl"
    now = datetime.now(timezone.utc)
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO records (file_key, line_num, uuid, request_id, "
                "ts, model, fresh_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, eph5_tokens, "
                "eph1h_tokens, cost_usd, is_canonical) VALUES "
                "(%s, 9100, %s, 'rollup-boundary', %s, "
                "'claude-sonnet-4-5', 1, 1, 0, 0, 0, 0, 0, TRUE)",
                (file_key, f"rollup-boundary-{now.timestamp()}", now),
            )
            cur.execute(
                "INSERT INTO tool_uses (file_key, line_num, idx, ts, "
                "tool_name, is_error, lines_added, lines_deleted) VALUES "
                "(%s, 9100, 0, %s, 'Edit', FALSE, 7, 3)",
                (file_key, now),
            )
        conn.commit()

    ingest.recompute_canonical()
    ingest.rebuild_rollup()
    ingest.rebuild_tool_rollup()

    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tool_uses SET lines_added = 70, lines_deleted = 30 "
                "WHERE file_key = %s AND line_num = 9100",
                (file_key,),
            )
        conn.commit()

    rolled = app_with_fresh_data.get(
        "/api/dashboard?range=30d&fresh=1"
    ).json()
    live = app_with_fresh_data.get(
        "/api/dashboard?range=1d&fresh=1"
    ).json()
    assert sum(h["lines_added"] for h in rolled["hourly"]) == 7
    assert sum(h["lines_deleted"] for h in rolled["hourly"]) == 3
    assert sum(h["lines_added"] for h in live["hourly"]) == 70
    assert sum(h["lines_deleted"] for h in live["hourly"]) == 30
```

Update `test_dashboard_hourly_carries_line_churn` to call
`ingest.rebuild_tool_rollup()` after raw insertion and add `fresh=1` to each
dashboard request. Its existing `(10, 4)` totals and project/model filter
assertions then exercise the rolled source.

- [ ] **Step 2: Run tests and observe RED**

Run: `.venv/bin/python -m pytest tests/test_api.py::test_dashboard_churn_uses_rollup_only_at_hourly_grain tests/test_api.py::test_dashboard_hourly_carries_line_churn -q`

Expected: FAIL because the 30-day response still reads the changed raw value
`(70, 30)` instead of the rolled snapshot `(7, 3)`.

- [ ] **Step 3: Add idempotent schema columns**

Add both columns to `CREATE TABLE tool_rollup`, then add:

```sql
ALTER TABLE tool_rollup ADD COLUMN IF NOT EXISTS
  lines_added BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tool_rollup ADD COLUMN IF NOT EXISTS
  lines_deleted BIGINT NOT NULL DEFAULT 0;
```

- [ ] **Step 4: Populate the sums during rebuild**

Extend the insert/select in `rebuild_tool_rollup` with:

```sql
lines_added, lines_deleted
SUM(tu.lines_added) AS lines_added,
SUM(tu.lines_deleted) AS lines_deleted
```

- [ ] **Step 5: Make churn source bucket-aware**

Change `_churn_source` to accept `use_rollup: bool` and return
`tuple[str, list, str]`. The rollup leg filters `tool_rollup tu` on
`tu.hour >= date_trunc('hour', %s::timestamptz)` and returns `"tu.hour"`;
the live leg preserves the current joins/filter and returns `"tu.ts"`.
Compute `use_rollup = bucket_s >= 3600` once in `_dashboard_sources`, pass it
to both source builders, store `churn_ts`, and replace both `tu.ts` references
in the churn bucket expression with `{src["churn_ts"]}`.

- [ ] **Step 6: Run API and ingest verification**

Run: `.venv/bin/python -m pytest tests/test_api.py tests/test_ingest.py -q`

Expected: all API and ingest tests pass.

- [ ] **Step 7: Commit the fix and close #16**

```bash
git add backend/api_dashboard.py backend/ingest.py backend/schema.sql tests/test_api.py
git commit -m "perf(dashboard): serve line churn from rollup" \
  -m "Closes #16" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 6: Full verification, one push, and CI monitoring

**Files:**
- Verify: all tracked changes and commits from Tasks 1–5.

**Interfaces:**
- Consumes: local CI toolchain and GitHub Actions workflows.
- Produces: one pushed `master` batch with every requested issue closed by commit messages and green remote checks.

- [ ] **Step 1: Re-read issue bodies/comments and inspect commit trailers**

Run:

```bash
for n in 10 12 13 14 15 16; do gh issue view "$n" --comments; done
git log --format=full -7
git diff origin/master...HEAD --check
```

Expected: every issue requirement maps to a change or verified pre-existing
implementation; every new commit has the requested coauthor; resolving commits
have the correct `Closes` lines.

- [ ] **Step 2: Run the complete pytest workflow**

Run: `.venv/bin/python -m pytest tests/ -q --tb=short -ra`

Expected: zero failures.

- [ ] **Step 3: Run the complete type workflow**

Run: `.venv/bin/pyright`

Expected: zero errors.

- [ ] **Step 4: Run both Python lint gates**

Run: `git ls-files '*.py' | xargs .venv/bin/pylint`

Run: `git ls-files '*.py' | xargs .venv/bin/pycodestyle`

Expected: both commands exit zero.

- [ ] **Step 5: Run the complete frontend lint workflow**

Run: `npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'`

Expected: zero errors.

- [ ] **Step 6: Push the complete batch once**

Run: `git push origin master`

Expected: one push advances `origin/master` to the verified local tip.

- [ ] **Step 7: Monitor the pushed commit's checks**

Use `gh run list --commit "$(git rev-parse HEAD)"` to discover all workflows,
then wait for tests, types, lint, and eslint to complete. Inspect logs for any
failure. Fix and re-verify only a regression caused by this batch; open a new
issue for a distinct reproducible bug and leave its implementation out.

- [ ] **Step 8: Confirm issue closure and clean synchronization**

Run:

```bash
gh issue list --state open --limit 100
git status --short --branch
```

Expected: #12–#16 are no longer open except for any separately filed bug,
owner-deferred #10 remains open, the worktree is clean, and local `master`
matches `origin/master`.
