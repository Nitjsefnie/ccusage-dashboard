# Activity Heatmap panel — design spec (2026-07-13)

New dashboard panel: **Activity Heatmap** — a 7×24 grid (weekday rows Mon–Sun ×
hour-of-day columns 0–23) of Claude Code activity, computed in **Czech local
time (`Europe/Prague`), summertime-aware**.

## Decision

Add a dedicated backend endpoint `GET /api/activity-heatmap` that does the
weekday×hour grouping in SQL via `AT TIME ZONE 'Europe/Prague'`, and a
self-fetching frontend panel gated on `backendOn` (same pattern as
`ToolUsagePanel` / `ReplyLatencyPanel` / `ToolErrorRatePanel`).

**Rejected alternatives:**

- *Client-side from dashboard `events`* — backend-mode events are adaptive time
  buckets (up to 1 day wide, `ts` = bucket center, see `_bucket_seconds` in
  `backend/api.py`). At long ranges the bucket is 1 day, which destroys
  hour-of-day information entirely. Not viable.
- *Fixed UTC+1/+2 offset math* — reimplements DST transition rules by hand.
  Postgres tzdata already knows the exact CET↔CEST transition instants;
  `AT TIME ZONE` is the canonical, maintained answer.

## Backend — `GET /api/activity-heatmap`

- Query params `range` (default `"30d"`), `project`, `model` — semantics,
  validation (`_parse_range` → 400 on garbage), auth and guest behaviour
  **mirror `tool_usage`** exactly. Decorated with `@cache_response`.
- Module constant `HEATMAP_TZ = "Europe/Prague"`, passed as a **bound SQL
  parameter** (`%s`), never interpolated.
- Cross-file uuid dedup at read time (SV-PARSER-SPEC): same
  `DISTINCT ON (r.uuid) … UNION ALL … uuid IS NULL` body as the `dashboard`
  endpoint's `dedup_body`, filtered by `ts >= since`, optional
  `f.project_id = %s` and `r.model LIKE %s`.
- Aggregation:

  ```sql
  SELECT EXTRACT(ISODOW FROM (d.ts AT TIME ZONE %s))::int AS dow,   -- 1=Mon … 7=Sun
         EXTRACT(HOUR   FROM (d.ts AT TIME ZONE %s))::int AS hour,  -- 0 … 23
         COUNT(*)            AS requests,
         SUM(d.output_tokens) AS output_tokens,
         SUM(d.cost_usd)      AS cost_usd
  FROM deduped d
  WHERE d.ts IS NOT NULL
  GROUP BY 1, 2
  ```

- Response (sparse — only non-empty cells):

  ```json
  {"range": "30d", "tz": "Europe/Prague",
   "cells": [{"dow": 1, "hour": 9, "requests": 42,
              "output_tokens": 12345, "cost_usd": 1.23}, …]}
  ```

### DST semantics (the load-bearing bit)

`records.ts` is `timestamptz`; `d.ts AT TIME ZONE 'Europe/Prague'` converts
through Postgres tzdata — UTC+1 in winter (CET), UTC+2 in summer (CEST), with
transitions at the true instants (last Sunday of March / October). No offset
constants anywhere in the code. Edge behaviour is inherently correct:
spring-forward day has no local hour 02 (that hour doesn't exist); fall-back
day maps two UTC hours onto local hour 02 (it happens twice).

## Frontend — `ActivityHeatmapPanel`

- Lives in `src/dashboard-charts-extra.jsx`, exported as
  `window.ActivityHeatmapPanel`. Mounted in `Dashboard` (`src/app.jsx`) inside
  `{backendOn && <div className="dash-heatmap">…}` after the Tool Error Rate
  panel. Props `{models, project, range, nonce}` — identical contract to
  `ToolUsagePanel`, including the per-panel model `<select>` built from the
  deduped short-model list.
- Refetches on `[project, range, activeModel, nonce]` (SSE `ingest_done` bumps
  `nonce` upstream — live updates for free).
- **Metric toggle**: Requests (default) / Output tokens / Cost — three radio
  chips; the toggle switches which metric drives cell intensity.
- **Grid**: SVG, 7 rows Mon→Sun top-to-bottom (English labels, Monday first —
  Czech week convention), 24 columns `0`–`23`. ~2px gaps between cells,
  slightly rounded cells, ResizeObserver-driven width like sibling panels.
- **Color** (dataviz rules): sequential single-hue ramp — theme surface at 0 up
  to one accent hue at max; intensity `sqrt(v / max)` (activity counts are
  heavy-tailed; sqrt keeps the mid-range readable). Zero cells: surface fill
  with a faint border so the grid stays visible. Cell values never rendered as
  always-on text — hover only. Axis/label text uses theme text colors, never
  the accent.
- **Hover tooltip** (`window.DashTooltip`): `Tue 14:00–15:00 · 123 requests ·
  45.6k output tok · $1.23` — all three metrics shown regardless of toggle.
- **Legend/header**: small min→max gradient strip with the max value labeled,
  and a header note `Europe/Prague (CET/CEST)` so the timezone is explicit.
- The `.dash-heatmap` wrapper div needs no CSS rule — sibling wrappers
  (`.dash-tools`, `.dash-latency`) have none either; the panel styles
  itself inline.

## Tests (`tests/test_api.py`)

1. **Shape**: on `app_with_data` (r2_mini), `/api/activity-heatmap?range=3650d`
   returns 200 with `tz == "Europe/Prague"`, every cell `dow ∈ 1..7`,
   `hour ∈ 0..23`, ints ≥ 0.
2. **Dedup consistency**: sum of `cells[].requests` equals the `requests` sum
   from `/api/dashboard` hourly rows for the same range (both read through the
   same dedup).
3. **DST correctness** (the reason this panel exists): insert two synthetic
   records directly into the test DB —
   `2026-01-15T10:30:00Z` (winter, CET = UTC+1, a Thursday) must land in
   `dow=4, hour=11`; `2026-07-15T10:30:00Z` (summer, CEST = UTC+2, a
   Wednesday) must land in `dow=3, hour=12`.
4. **Filters**: `model=` substring and `project=` filters subset the cells
   (mirror existing filter tests).
5. **Bad range** → 400.

## Docs

Add the panel to the feature lists in `README.md` and `AGENTS.md`.

## Out of scope

- **Offline (drag-drop) heatmap** — consistent with the three newest panels,
  which are backend-only. Offline per-record events do have full fidelity, so
  this can be added later without redesign.
- **Configurable timezone** — YAGNI; the operator is in CZ. `HEATMAP_TZ` is a
  single constant, trivially parameterizable later.
- **`/api/export` PNG parity** — the matplotlib export script doesn't know
  this panel; separate effort if ever wanted.

## Files touched

`backend/api.py`, `src/dashboard-charts-extra.jsx`, `src/app.jsx`,
`tests/test_api.py`, `README.md`, `AGENTS.md`.

No schema change, no parser change → **no `PARSER_VERSION` bump**.
