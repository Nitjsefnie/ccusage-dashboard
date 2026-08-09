# Time-series Right-edge Geometry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every time-series bar, cumulative point, and hover interval inside the plot while faithfully representing backend aggregate timestamps as bucket centers.

**Architecture:** `backendDashToShape` reconstructs complete server-bucket coverage from the first and last center timestamps. `TimeSeriesPanel` builds bounded intervals and derives bar rectangles, cumulative points, tooltip ranges, and hover selection from those intervals. Pure JavaScript helpers make the temporal contract executable through Node without mocking React or SVG.

**Tech Stack:** React 18 JSX served through in-browser Babel, SVG, Node-based JavaScript probes under pytest, pytest

## Global Constraints

- Work directly on `master`, as explicitly requested by the repository owner.
- Preserve the display-bin floor at `bucket_s`; never split a backend aggregate across finer visual bins.
- Do not use SVG clipping as the primary fix and do not hide or truncate the final aggregate.
- Do not add an offline upload path; claudit is backend-only.
- Keep the existing palette, typography, gutters, and 90% bar fill ratio.
- Every commit includes `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`.
- The completing implementation commit includes `Closes #17`.
- Issue #10 is already implemented and closed; unrelated defects become separate issues.

---

## File Structure

- Create `tests/test_time_series_geometry.py` to execute the real helpers from shipped JSX and assert bucket coverage plus bounded rendering geometry.
- Modify `src/app.jsx` to add `backendAggregateRange(events, bucketS)` and use it in `backendDashToShape`.
- Modify `src/dashboard-charts.jsx` to add bounded interval, series-data, coordinate, rectangle, and hit-test helpers and route `TimeSeriesPanel` through them.

### Task 1: Reconstruct backend aggregate coverage

**Files:**
- Create: `tests/test_time_series_geometry.py`
- Modify: `src/app.jsx` near `backendDashToShape`

**Interfaces:**
- Consumes: event objects with numeric millisecond `ts`; dashboard `bucket_s` in seconds.
- Produces: `backendAggregateRange(events, bucketS) -> {start: number, end: number}`.

- [ ] **Step 1: Write the failing range and adapter tests**

Extract `backendAggregateRange` from `src/app.jsx`. Assert that centers at 3,
9, and 15 hours with `bucketS=21600` produce `{start: 0, end: 18 hours}` and
that missing metadata preserves `{start: 3 hours, end: 15 hours + 1 ms}`.
Execute `backendDashToShape` with the same rows and assert it returns the full
coverage.

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_time_series_geometry.py -q`

Expected: FAIL because `backendAggregateRange` is absent.

- [ ] **Step 3: Implement aggregate coverage**

Add:

```javascript
function backendAggregateRange(events, bucketS) {
  const first = events[0].ts;
  const last = events[events.length - 1].ts;
  const bucketMs = Number(bucketS) * 1000;
  if (Number.isFinite(bucketMs) && bucketMs > 0) {
    return { start: first - bucketMs / 2, end: last + bucketMs / 2 };
  }
  return { start: first, end: last + 1 };
}
```

Use `const range = backendAggregateRange(events, b.bucket_s)` and return that
object from `backendDashToShape`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_time_series_geometry.py tests/test_dashboard_binning.py -q`

Expected: PASS, including the unchanged backend bucket floor.

- [ ] **Step 5: Commit the adapter correction**

Commit `src/app.jsx` and the range tests as `fix(charts): reconstruct aggregate bucket coverage` with the required co-author trailer.

### Task 2: Bound rendered and interactive geometry

**Files:**
- Modify: `tests/test_time_series_geometry.py`
- Modify: `src/dashboard-charts.jsx` before and inside `TimeSeriesPanel`

**Interfaces:**
- Produces: `boundedTimeIntervals(range, binMs) -> Array<{start, end}>`.
- Produces: `buildTimeSeriesData(events, range, binMs, valueOf) -> {bins, cumPts, total}`.
- Produces: `timeX(ts, range, padL, plotW) -> number`.
- Produces: `timeBarRect(bin, range, padL, plotW) -> {x, width}`.
- Produces: `timeBinIndexAtX(bins, range, padL, plotW, x) -> number`, returning `-1` outside the plot.

- [ ] **Step 1: Add failing bounded-geometry tests**

Probe `range={start:0,end:10}` with `binMs=6`, two events, `padL=60`, and
`plotW=400`. Assert bins `[0,6)` and `[6,10]`, rectangles
`{x:60,width:216}` and `{x:300,width:144}`, cumulative x coordinates
`[60,300,460]`, total `5`, and hover results
`[-1,0,1,1,1,-1]` for x coordinates `[59,60,300,459,460,461]`.

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_time_series_geometry.py -q`

Expected: FAIL because the chart helpers are absent.

- [ ] **Step 3: Port the sibling helpers**

Add validation and bounded interval construction, aggregate sorted events into
those intervals, derive cumulative points at bounded interval ends, scale time
with `timeX`, compute each bar from its actual interval width, and select hover
bins only inside the plot.

- [ ] **Step 4: Route `TimeSeriesPanel` through the helpers**

Replace manual bin/cumulative loops and constant `barW`; use
`buildTimeSeriesData`, `timeBarRect`, and `timeBinIndexAtX`. Add stable
`data-plot-boundary`, `data-time-bar`, and `data-cumulative-line` hooks for
rendered-DOM measurement. Update the right-axis comment so it no longer claims
bars intentionally occupy the gutter.

- [ ] **Step 5: Run focused tests and JavaScript lint**

Run:

```bash
.venv/bin/python -m pytest tests/test_time_series_geometry.py tests/test_dashboard_binning.py -q
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'
```

Expected: all tests pass and ESLint exits 0.

- [ ] **Step 6: Commit the completing geometry fix**

Commit `src/dashboard-charts.jsx` and the expanded tests as
`fix(charts): keep time-series bars inside plot bounds` with `Closes #17` and
the required co-author trailer.

### Task 3: Verify and publish

**Files:** none beyond Tasks 1–2.

**Interfaces:**
- Consumes: the completed port on `master`.
- Produces: one verified batched push whose remote workflows are green.

- [ ] **Step 1: Run issue #10 regression coverage**

Run the parser line-churn tests and the dashboard live/rollup line-churn API
tests to verify the already-complete feature remains intact.

- [ ] **Step 2: Run every local gate**

Run full pytest, pyright, pylint, pycodestyle, ESLint, and
`git diff --check origin/master..HEAD`. Require every command to exit 0.

- [ ] **Step 3: Audit commit metadata**

Verify every commit after `origin/master` contains the exact co-author trailer
and the completing commit contains `Closes #17`.

- [ ] **Step 4: Push once and monitor CI**

Fetch/rebase if the remote moved, rerun the full suite after any rebase, push
`master` once without force, and require every remote workflow to succeed.

- [ ] **Step 5: Confirm closure and synchronization**

Confirm #17 is closed, #10 remains closed, and local `master` is clean and
synchronized with `origin/master`.
