# Active Issues Resolution Design

## Scope

Resolve every issue open on `Nitjsefnie/claudit` as of 2026-08-09: #10 and
#12–#16. Read the complete issue bodies and comment histories before design.
Issues #13 and #14 each have an owner comment replacing stale historical
commit ranges; those ranges are background only and do not affect the current
file-level diagnosis.

Work directly on the repository's default `master` branch, commit each logical
fix separately, and push the completed batch once. Every authored commit must
carry `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`. An implementation
commit that resolves an issue must also carry `Closes #<number>`.

If implementation reveals a distinct, reproducible defect, file it as a new
remote issue with evidence and leave its implementation out of this batch.
Do not turn adjacent cleanup or speculative improvements into scope.

## Current-state findings

- #10 is already implemented across parser, storage, API, and UI by commits
  `60437d6`, `993f736`, `fcb85a8`, and `3d1db46`. Its focused parser tests and
  dashboard API test pass on current `master`; it remains open because no
  commit closed it.
- #12 reproduces with one undated and one timezone-aware record:
  `_build_ctx_turns` compares naive `datetime.min` with an aware timestamp and
  raises `TypeError`.
- #13 reproduces by retaining an event loop in `events._main_loop`, closing
  it, and calling `broadcast_threadsafe`; `call_soon_threadsafe` raises
  `RuntimeError: Event loop is closed`.
- #14's current tree has two duplicated cookie-issuance blocks (authenticated
  and guest login), not the three described when the issue was filed. The
  structural defect remains: issuance flags have no single owner.
- #15 has two independent frontend bucket selectors. Clamping only the
  dashboard selector, as the issue suggests, leaves Cache TTL broken because
  its `Math.min(chosen, binMs)` can select a bin finer than server aggregation.
- #16 is confirmed by source selection: churn uses `tool_uses` at both
  5-minute and hourly-or-coarser display widths, while other additive panels
  switch to rollups at one hour.

## Design

### #12 — deterministic mixed-timestamp context ordering

Define the intended ordering explicitly: records with no timestamp sort
before dated records; records within each partition retain chronological then
line-number ordering. Use a tuple key whose first element partitions missing
timestamps, preventing Python from ever comparing a naive sentinel against an
aware timestamp. This preserves the intent of the existing `datetime.min`
fallback rather than silently changing undated records to sort last or be
discarded.

Add a fixture-driven parser regression with a sub-1 KB JSONL containing an
undated usage record, a user boundary, and a dated usage record. The test must
first reproduce the crash, then assert the two resulting context turns and
their order. Because stored `ctx_turns` can change for affected files, bump the
tracked deployment `PARSER_VERSION` from 19 to 20 so the next ingest reparses
history.

The read-only canonical analyst script has the same underlying assumption
about timestamps. This repository must not edit that external file; the local
issue is fixed and the parity limitation is called out rather than hidden.

### #13 — broadcaster lifecycle teardown

Add an explicit `events.clear_loop()` lifecycle operation that clears both the
captured loop and shutdown event. In application teardown, preserve the
load-bearing order:

1. signal SSE shutdown while the captured loop is still usable;
2. stop the scheduler with its existing non-blocking behavior;
3. clear broadcaster lifecycle state before FastAPI's loop closes.

`broadcast_threadsafe` must snapshot the loop into a local variable before
checking or calling it. It should no-op when that snapshot is absent or closed,
and tolerate the narrow race in which the loop closes between the check and
`call_soon_threadsafe`. This makes both post-clear and already-closed-loop
broadcasts safe without swallowing unrelated errors.

Tests cover a closed-loop late broadcast and the lifespan teardown ordering,
so moving the clear before `signal_shutdown` cannot regress silently.

### #14 — one session-cookie issuance contract

Add `session.set_session_cookie(response, token)` as the sole owner of session
cookie issuance. It sets the cookie name, `HttpOnly`, environment-controlled
`Secure`, `SameSite=Strict`, seven-day `Max-Age`, and `Path=/`, while omitting
`Domain`. Both authenticated and guest login routes call this helper. Logout
continues to use `delete_cookie`, because expiration is a different contract.

Tests pin the helper's complete `Set-Cookie` attributes and verify both login
paths issue through the same contract. The helper stays in `backend/session.py`
to avoid an import cycle: `login.py` already depends on `session.py`.

### #15 — server-aware shared bin-width selection

Create `src/dashboard-binning.js`, a plain classic script with pure helpers:

- `pickAdaptiveBinMs(spanMs)` implements the existing nice-width ladder;
- `dashboardBinMs(range, bucketS)` returns the larger of the adaptive width
  and the server's `bucket_s` expressed in milliseconds;
- `cacheTtlBinMs(range, dashboardMs)` returns the larger of its data-local
  adaptive width and the already server-clamped dashboard width.

Load the helper before Babel JSX modules. `Dashboard` consumes
`window.dashboardBinMs`; `CacheTTLPanel` consumes `window.cacheTtlBinMs`.
Synthetic preview data has no server bucket, so `dashboardBinMs` falls back to
the existing adaptive behavior when `bucketS` is absent, non-finite, or
non-positive.

Drive the real helper file through Node in pytest. Cover the reported six-day
data span under 7-day/30-day server buckets, the short-data 24-hour mirror
case, Cache TTL's secondary picker, and the no-backend synthetic fallback.
The result must keep bars and axis captions at the actual aggregation width;
no API or cache-key change is required.

### #16 and closure of #10 — line churn rollup

Extend `tool_rollup` with non-null `lines_added` and `lines_deleted` bigint
sums. Put the columns in both `CREATE TABLE IF NOT EXISTS` and idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements so fresh and existing
databases converge. `rebuild_tool_rollup` sums the two raw `tool_uses` columns
inside its existing hour/project/model/tool grouping.

Make churn source selection accept the display bucket width:

- at `bucket_s >= 3600`, read `tool_rollup`, include the partial boundary hour
  using the same `date_trunc('hour', since)` rule as `usage_rollup`, and apply
  project/model filters directly to rollup columns;
- below one hour, retain the live `tool_uses` path and its exact timestamp
  filter, including the records join needed only for a model filter.

The final churn query uses the source's supplied timestamp expression so both
paths share bucketing and response folding. With no model filter, rollup sums
all models including `model=''`, retaining usage-less tool calls. A model
filter excludes those rows, matching the live inner-join semantics.

Update the API regression to rebuild derived state after inserting tool calls,
then verify totals and project/model filtering through the rollup path. Add a
source-selection regression that pins live behavior below one hour and rollup
behavior at or above one hour. The related implementation commit closes both
#16 and the already-delivered #10.

## Error handling and migrations

- Parser malformed-line behavior remains unchanged; only valid usage records
  with a missing timestamp are reordered safely.
- A late SSE notification after lifecycle teardown is intentionally dropped;
  shutdown has no live browser consumer to notify.
- Cookie security defaults remain unchanged (`COOKIE_SECURE=1` outside tests).
- New rollup columns default to zero until the next rebuild. Normal deployment
  reapplies the schema and restarts the service; startup ingest rebuilds
  derived state before cached dashboard data is considered current.

## Verification and delivery

For every production change, add the regression first and observe the expected
failure before implementation. Run focused tests after each fix and the full
CI-equivalent gates before the one batch push:

```bash
python -m pytest tests/ -q --tb=short -ra
pyright
git ls-files '*.py' | xargs pylint
git ls-files '*.py' | xargs pycodestyle
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'
```

After pushing, monitor all GitHub Actions workflows on the pushed `master`
commit. Fix only failures caused by this batch. File a distinct issue for any
newly reproduced unrelated defect and leave its code unchanged.
