# Tool Error Rate Panel — Design

**Date**: 2026-05-08
**Status**: Approved (design phase)
**Owner**: claudit

## Goal

Add a dashboard panel showing tool-call error rate as an EMA over time,
broken out per model, with per-tool drill-down via checkboxes. Mirrors
the layout of the existing Per-Session Context Growth panel.

## Definitions

- **Tool error**: a `tool_result` content block (in user-message content)
  whose `is_error` field is `true`. Regex/text-match detection on the
  result body is explicitly excluded — only `is_error === true` counts,
  matching the actual harness-flagged failure counter.
- **Settled tool call**: a `tool_use` for which a matching `tool_result`
  (by `tool_use_id`) appears later in the same JSONL file. `is_error` is
  set to `true` or `false`.
- **Unsettled tool call**: no matching `tool_result` found in the file.
  `is_error` stays NULL. Excluded from both numerator and denominator.
- **Error rate**: `n_error / n_total` over settled calls in a bucket,
  per (model, tool_name).

## Layout

Panel structure mirrors `ContextGrowthPanel` in
`src/dashboard-charts-extra.jsx`:

- One sub-panel per model present in the range.
- Sub-panels arranged in rows of 2 (responsive width).
- Each sub-panel is self-contained (its own checkbox row, its own SVG,
  its own y-axis scale).

Per sub-panel:

- **Series**:
  - `Aggregate` — error rate across all tools for this model.
  - One line per distinct tool name observed in this model.
- **Defaults**: `Aggregate` ON + top-3 tools by `n_total` for this model
  *over the visible range* ON; remaining tools OFF. Default re-evaluates
  when the range or project filter changes (the user's explicit per-tool
  toggles override the default in the same way `ContextGrowthPanel`
  layers explicit overrides over its top-2 default).
- **Rendering**: each visible series rendered as a single EMA polyline
  (alpha = 0.15, matching the burn-rate panel).
- **Y axis**: 0 → max EMA across visible series in this sub-panel,
  with a small headroom pad. Auto-scaling per sub-panel keeps low rates
  legible (a fixed 0–100% would compress most data to near-zero).
- **X axis**: time, same range as the rest of the dashboard.
- **Hover tooltip** (per bucket): bucket window, n_total, n_error, rate
  %, EMA %.

## Data model

### Schema change

`backend/schema.sql`:

```sql
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS is_error BOOLEAN;
```

Idempotent migration; no index needed (per-bucket aggregation is fast
on the existing `tool_uses_ts_idx`).

### `PARSER_VERSION` bump

Bump in `.env` so every existing file reparses on next ingest and
backfills `is_error`.

## Parser change (`backend/parse.py`)

Single line-walk extension:

1. **On assistant messages**: capture each `tool_use.id` (currently we
   only capture `name`) on the corresponding in-memory `tool_uses`
   entry. The `id` is the assistant-emitted UUID that user-side
   `tool_result` blocks reference via `tool_use_id`. This field stays
   in-memory only — it's used to fill `is_error` before insert and is
   *not* persisted to the DB.
2. **On user messages with list content**: walk `content[]` for blocks
   where `type === "tool_result"`. For each, store
   `result_map[tool_use_id] = bool(is_error)`. (Today the parser only
   handles string-content user messages.)
3. **After the line walk**: for each entry in `tool_uses`, set
   `is_error = result_map.get(id, None)`. Unmatched stays NULL.

Phase 1 dedup (per-`requestId` first-occurrence) already filters
streaming dupes of the assistant `tool_use` blocks, so each tool call
appears in `tool_uses` exactly once. Tool results in user messages are
not subject to streaming duplication, so we walk every user-message
list-content occurrence.

## API

New endpoint in `backend/api.py`:

```
GET /api/tool-error-rate?range=...&project=...&model=...
```

- `range`: same vocabulary as siblings (24h / 7d / 30d / 90d / 1y / all).
- `project`: optional project_id filter.
- `model`: optional substring filter (parity with `/api/tool-usage`,
  used when frontend wants to narrow; not strictly required since the
  panel groups by model anyway).
- Bucket size from existing `_bucket_seconds(delta)`.
- SQL: select bucket center, model (joined via `records` on
  `(file_key, line_num)`), tool_name, total count, error count, where
  `is_error IS NOT NULL`. Filter by project via `files`.

Response shape:

```json
{
  "range": "30d",
  "project": null,
  "bucket_s": 3600,
  "buckets": [
    {"ts": "...", "model": "claude-opus-4-7", "tool": "Bash",
     "n_total": 42, "n_error": 3},
    ...
  ]
}
```

Cross-file uuid dedup is **not** applied — `tool_uses` aren't keyed on
`records.uuid`; they're per `(file_key, line_num, idx)`. Same-file is
the natural dedup boundary for tool calls.

## Frontend

### New component

`src/dashboard-charts-extra.jsx`: `ToolErrorRatePanel`, exported as
`window.ToolErrorRatePanel`. Layout mirrors `ContextGrowthPanel`:

- Reads buckets from `/api/tool-error-rate`.
- Groups by model. For each model:
  - Computes per-tool series and an aggregate series (sum of all tools
    in that model bucket-wise).
  - Computes EMA(α=0.15) over the per-bucket rate sequence for each
    visible series.
- Renders sub-panels in rows of 2 with their own checkbox row.

### Wiring

`src/app.jsx`:

- Add a `toolErrorRate` fetch alongside the existing `toolUsage` fetch
  (same `range`, `project`, `model` selectors already in place).
- Mount `ToolErrorRatePanel` after the reply-latency panel.

## Failure modes

- **No tool calls in range**: panel renders a single placeholder ("no
  tool calls in range") instead of empty sub-panels.
- **Sparse buckets** (`n_total === 0` for a series in a bucket): the
  bucket contributes no point to that series' EMA sequence. The
  polyline is plotted at the actual non-sparse bucket centers; gaps
  between non-sparse buckets render as straight polyline segments
  (no zero-injection that would pull the EMA toward 0).
- **Tool with very few calls**: still rendered when checked, but the
  EMA will be dominated by single-event 0/1 transitions; that's
  acceptable — the user can uncheck noisy tools.
- **Active sessions / tailing files**: if the assistant has emitted a
  `tool_use` but no `tool_result` is in the file yet, the row is NULL
  and excluded. On the next ingest run, the file's etag changes,
  records and tool_uses are reparsed, and `is_error` settles.

## Out of scope

- Per-tool "Other" rollup (non-top-3 are checkbox-accessible, not
  bucketed into Other).
- Severity classes / error-text clustering.
- Retry tracking (a tool retried after an error counts as a separate
  call here).
- Per-bucket outlier dots, IQR ribbons, percentile lines — EMA only,
  per the original request.

## Test plan

Pytest fixtures (`tests/test_parse.py` style):

- A fixture with one `tool_use` + matching `tool_result` `is_error:true`
  → `tool_uses` row has `is_error=true`.
- Same with `is_error:false` → `is_error=false`.
- A fixture with `tool_use` and no matching `tool_result` →
  `is_error IS NULL`.
- A fixture with `tool_result` referencing an unknown `tool_use_id`
  → silently dropped (no row created).
- Streaming dup of an assistant message: only the first occurrence
  contributes to `tool_uses`, and the `is_error` from the matching
  `tool_result` lands on it.

API smoke test: `/api/tool-error-rate` returns expected shape against
the mini R2 mirror.
