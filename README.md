# claudit

**Claude Code Usage Dashboard** — a self-hosted web app for visualising Claude
Code session JSONL transcripts.

A FastAPI backend ingests transcripts from Cloudflare R2 (or a local `file://`
mirror), parses them into Postgres, and serves dashboards and raw transcripts
to a React + in-browser-Babel frontend (no build step).

The dashboard panel set — Session Burn Rate, Cost by Model, Token Breakdown,
Prompt-Cache TTL Split, Per-Session Context Growth, Response Sizes, Tool Usage,
Reply Latency, Tool Error Rate — is based on
[`nhz-io/ccusage-plot`](https://github.com/nhz-io/ccusage-plot). This repo
ports those matplotlib-only offline visualisations into a hosted SVG/React app
with multi-user auth, R2 ingest, and live updates.

## Features

- **Cost-by-model** breakdown with the canonical 5-minute / 1-hour
  cache-create TTL split (`ephemeral_5m` × 1.25× base, `ephemeral_1h` × 2× base).
- **Token Breakdown** as paired sort-by-tokens / sort-by-cost bars
  over Input, Output, Cache Create (5m / 1h / unsplit), Cache Read.
- **Prompt-Cache TTL Split** showing adaptively-bucketed `ephemeral_5m`
  vs `ephemeral_1h` cache_create volumes with a 5m-share-% trend strip.
- **Response Sizes by Model** — adaptively-bucketed median + p90 of
  *visible response characters* (text content blocks; thinking excluded)
  on a log y-axis, per-model checkboxes.
- **Per-Session Context Growth** — per-model sub-panels with a
  p25–p75 IQR ribbon under a median line plus faint per-session
  traces, a multi-model checkbox-driven comparison row, and
  per-FILE traces so sub-agent invocations surface under their own
  model even when no main session JSONL exists. Each trace is
  anchored at an implicit (turn 0, ctx 0) origin.
- **Session burn rate** scatter with dot **area** scaling by
  end-of-session context size, model-coloured, plus EMA lines for
  output/input/cache-create/cache-read tokens-per-hour.
- **Tool Usage Ratio over Time** — adaptively-bucketed
  stacked-area-to-100% per tool (bucket size = largest in [60s, 1d]
  yielding ≥100 bins across the range) with top-N-at-any-bucket band
  promotion (so emerging tools don't get hidden in `Other`),
  per-panel model select, a per-bucket `Other` breakdown on hover,
  and `server_tool_use` blocks (e.g. WebSearch) counted alongside
  client tool calls.
- **Tool Error Rate over Time** — per-model EMA progression with
  per-tool toggleable lines (top-3 default ON), plus an Aggregate line.
- **Reply Latency over Time** — per-(bucket, model) p10–p90 ribbon
  with a median line and top/bottom 1% outlier dots (only when the
  bucket has ≥100 replies); log y-axis from 0.1s to max p90. Latency
  is the gap from each anchored user message to its assistant reply,
  computed at parse time (instrumentation/bash-IO and interrupt-marker
  user messages don't anchor a window).
- **Activity Heatmap** — weekday × hour grid of request activity in
  Czech local time (`Europe/Prague`, DST-aware via Postgres
  `AT TIME ZONE`), with requests / output-tokens / cost metric toggle
  and a per-panel model filter, plus Σ margin totals per weekday and per hour.
- **Cross-file uuid dedup** at query time so sub-agent JSONLs roll
  into their parent session without double-counting.
- **Rate-limit hit** detection (Claude Code's `out of extra usage`
  marker on `type:"assistant"` records).
- **Time-range picker** (24h / 7d / 30d / 90d / 1y / all).
- **Live updates**: server-sent `ingest_done` events trigger a
  data refetch — no page reload.
- **Auth**: user-id + password against an external auth DB's
  `users.config` PBKDF2 hashes, OR a guest mode (read-only, no project
  filtering, no per-session detail).

## Architecture

```
R2 (claude bucket)
  ↓  hourly ingest  (APScheduler @ :15 UTC, or POST /admin/ingest)
Postgres `claudit`
  • projects     (project_id PK)
  • files        (file_key PK, ctx_turns JSONB, rate_limit_hits JSONB)
  • records      (file_key, line_num PK, per-request tokens + cost
                  + text_chars for visible-response size
                  + reply_latency_s for the user→assistant gap)
  • tool_uses    (file_key, line_num, idx PK, ts, tool_name, is_error)
  • ingest_runs  (audit log)
  ↓  on-demand
FastAPI  →  /api/dashboard, /api/cache, /api/context-growth/*,
            /api/sessions, /api/sessions/{id}/transcript,
            /api/tool-usage, /api/tool-error-rate,
            /api/reply-latency, /api/models,
            /api/events, /api/export
  ↓
React + in-browser Babel  →  /  (served by FastAPI)
```

`backend/parse.py` mirrors `~/.claude/scripts/parse_session.py` for
Phase 1 within-file `requestId` max-merge; cross-file uuid dedup
(Phase 2) is performed at query time via `DISTINCT ON (uuid)` in the
read endpoints. Costs are pre-computed at ingest using
`backend/pricing.py` (single source of truth — bump `PARSER_VERSION`
in `.env` when rates change to force a full reparse).

## Quick start

```bash
createdb claudit
psql claudit -f backend/schema.sql
cp backend/.env.example .env  # edit values
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

The first request blocks while ingest runs (~30s on a warm DB,
several minutes for a cold cache against the full `claude` bucket).
Subsequent ingests are incremental (etag + parser_version check per
file). `POST /admin/ingest` with the `X-Admin-Token` header forces an
out-of-band run.

For local dev without R2 credentials, point `R2_ENDPOINT` at a
filesystem mirror (e.g. `R2_ENDPOINT=file:///tmp/r2/`) — the R2
client falls back to walking the directory tree.

## Operations

The deploy is intended to run under systemd. See
[`examples/claudit.service`](examples/claudit.service) for a sample
unit file. Key settings:

- `--timeout-graceful-shutdown 5` so SSE connections drain quickly.
- `TimeoutStopSec=10` for fast restarts.

```bash
systemctl restart claudit
systemctl status claudit
journalctl -u claudit -f
```

Schema migrations are idempotent — re-apply after editing
`backend/schema.sql`:

```bash
psql claudit -f backend/schema.sql
```

## Auth

Login expects a numeric user ID whose row in the auth DB's `users`
table has a PBKDF2 web-password hash stored under
`config.web_password_hash` (with a paired `web_password_salt`). The
hash format mirrors the constants in `backend/auth.py`
(SHA-256, 200,000 iterations, hex salt) so any external user-management
process that writes the same shape can issue credentials. Sessions
are HMAC-signed cookies with a 7-day TTL. A **Continue as guest**
button mints a read-only guest session (no project filter, no
per-session transcript access; cookie invalidates on every server
restart).

## Layout

- `backend/` — FastAPI app, auth, ingest, parser, R2 client, pricing,
  caches, schema, SSE broadcaster.
- `public/` — `index.html`, `app.css`. Served at `/`.
- `src/` — React JSX modules. Served at `/src/*` with mtime-based
  cache-bust.
- `scripts/` — `plots/ccusage_plot.py`, the upstream
  `nhz-io/ccusage-plot` reference. Canonical analyst scripts are not
  vendored here; invoke them by absolute path under `~/.claude/scripts/`.
- `tests/` — pytest suite (parser fixtures, ingest, API).
- `fixtures/` — small JSONL + zip samples for parser tests.
- `examples/` — sample systemd service file.

## Contributing

Issues and PRs welcome — including agent-authored ones. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, the test suite, and the
invariants most likely to trip a patch.

## License

MIT (see [`LICENSE`](LICENSE), © 2026 Nitjsefnie).
Third-party notices are in [`NOTICE`](NOTICE).

Original visual design and dot-scaling formulas adapted from
[`nhz-io/ccusage-plot`](https://github.com/nhz-io/ccusage-plot),
licensed MIT © 2026 Kumarajiva — see
[upstream LICENSE](https://github.com/nhz-io/ccusage-plot/blob/main/LICENSE).
