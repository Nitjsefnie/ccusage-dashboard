# session-viz

@README.md

## Repo orientation

- `backend/` — FastAPI app.
  - `app.py` — startup/shutdown, route mounting, `/` static, asset cache-bust.
  - `api.py` — REST endpoints (`/api/me`, `/api/projects`, `/api/dashboard`,
    `/api/cache`, `/api/context-growth/{agg,session}`, `/api/sessions*`,
    `/api/events` SSE).
  - `parse.py` — JSONL → records + `ctx_turns` + `rate_limit_hits`. Mirrors
    the canonical `~/.claude/scripts/parse_session.py` for Phase 1
    within-file `requestId` max-merge.
  - `pricing.py` — single source of truth for per-model rates. Bump
    `PARSER_VERSION` in `.env` whenever this changes.
  - `ingest.py` — R2 walk, etag/parser-version reparse decision, persistence
    in two-phase transactions, broadcasts `ingest_done` SSE on success.
  - `r2.py` — S3 client with `file://` filesystem-mirror fallback for dev.
  - `auth.py`, `login.py`, `session.py` — PBKDF2 verification against the
    external auth DB's `users.config`, HMAC-signed session cookies, plus
    a guest-mode sentinel (`user_id=0`, per-process secret).
  - `events.py` — thread-safe SSE broadcaster.
  - `db.py` — two psycopg pools: `viz_pool` (claude_viz) and `auth_pool`
    (read-only auth DB). Pools never join across DBs.
  - `cache.py` — in-memory LRU for raw transcript bytes.
  - `schema.sql` — idempotent `CREATE TABLE IF NOT EXISTS` + safe
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations.

- `public/` — `index.html`, `app.css`. Served at `/`. Backend rewrites
  `index.html` on each request to inject `window.BACKEND_URL`,
  `window.IS_GUEST`, and mtime-based `?v=` query strings on every static
  asset reference.

- `src/` — React JSX modules served at `/src/*` (in-browser Babel, no
  build step).
  - `app.jsx` — top-level shell, routing, dashboard fetcher, SSE listener.
  - `parser.js` — in-browser transcript parser used by the Inspector.
    Pricing table here MUST match `backend/pricing.py`.
  - `dashboard-charts.jsx`, `dashboard-charts-extra.jsx` — SVG panels.
  - `views/` — `cache-view.jsx`, `context-growth-view-v2.jsx`.

- `scripts/` — symlinks to canonical `~/.claude/scripts/*.py`. **Read-only**;
  the web app does NOT invoke them at runtime. They exist for parity
  with analyst tooling and so any walker looking for them in this tree
  finds them.

- `tests/` — pytest suite. `test_parse.py` is fixture-driven; add a
  fixture before changing parser behavior.

- `fixtures/` — small JSONL + zip samples (kept under 1 KB each); larger
  end-to-end mirrors live outside the repo.

## Conventions

- **Cost is always TTL-split**. `cache_creation` decomposes into
  `ephemeral_5m` (× 1.25 base) + `ephemeral_1h` (× 2 base). Tokens with
  no `ephemeral_*` split (legacy SDK) are charged at the 5m rate.
  Single-rate `cache_create` cost is banned.
- **Cross-file uuid dedup happens at READ time** via `DISTINCT ON (uuid)`
  in `/api/dashboard`, `/api/cache`, etc. Per-file `requestId` max-merge
  happens at INGEST time. There is no persisted Phase 2 rollup table.
- **Don't invoke `~/.claude/scripts/parse_session.py`** at runtime, and
  don't edit it from this repo. If the canonical Python and our port
  drift, fix it here, not there.
- **Tests use fixtures, not real R2.** The R2 client supports
  `R2_ENDPOINT=file:///path/to/mirror/` for offline dev.

## Operations

- Service: `systemctl restart session-viz` (unit at
  `/etc/systemd/system/session-viz.service`, runs uvicorn with
  `--timeout-graceful-shutdown 5`, `TimeoutStopSec=10`).
- Manual ingest: `POST /admin/ingest` with `X-Admin-Token: $ADMIN_TOKEN`.
- Schema migration after editing `backend/schema.sql`:
  `psql claude_viz -f backend/schema.sql` (idempotent).
- Bump `PARSER_VERSION` in `.env` whenever parser semantics or
  `pricing.py` rates change — every file reparses on next ingest.
