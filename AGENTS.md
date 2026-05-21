<!-- From: /root/session-viz/AGENTS.md -->
# ccudash

@README.md

## Project overview

**ccudash** (Claude Code Usage Dashboard) is a self-hosted web application that visualises Claude Code session JSONL transcripts. It ingests transcripts from Cloudflare R2 (or a local `file://` mirror), parses them into Postgres, and serves dashboards and raw transcripts to a React frontend rendered via in-browser Babel (no npm/build step).

The dashboard panels include: Session Burn Rate, Cost by Model, Token Breakdown, Prompt-Cache TTL Split, Per-Session Context Growth, Response Sizes, Tool Usage Ratio, Reply Latency, and Tool Error Rate.

## Technology stack

- **Backend**: Python 3.13+, FastAPI, Uvicorn, psycopg3 (with connection pooling)
- **Frontend**: React 18 (loaded from CDN), in-browser Babel transpilation, vanilla JS/JSX — no webpack, vite, or npm install
- **Database**: PostgreSQL (two separate DBs: `claude_viz` for app data, external auth DB for user credentials)
- **Object storage**: Cloudflare R2 via S3-compatible API, or local filesystem mirror (`file://`)
- **Scheduling**: APScheduler (BackgroundScheduler) for hourly ingest
- **Serialization**: orjson for fast JSON parsing
- **Testing**: pytest, pytest-asyncio, httpx (for TestClient)
- **Deployment**: systemd service (see `examples/ccudash.service`)

## Project structure

```
backend/          — FastAPI application
  app.py          — Startup/shutdown, route mounting, static asset serving,
                    index.html rewriting with cache-bust and auth injection
  api.py          — REST endpoints (/api/me, /api/projects, /api/dashboard,
                    /api/cache, /api/context-growth/{agg,session},
                    /api/sessions*, /api/events SSE, /api/tool-usage,
                    /api/tool-error-rate, /api/reply-latency, /api/models)
  parse.py        — JSONL → records + ctx_turns + rate_limit_hits.
                    Mirrors canonical ~/.claude/scripts/parse_session.py
                    for Phase 1 within-file requestId max-merge.
  pricing.py      — Single source of truth for per-model token rates (USD/M).
                    Bump PARSER_VERSION in .env whenever this changes.
  ingest.py       — R2 walk, etag/parser-version reparse decision, persistence
                    in two-phase transactions, broadcasts ingest_done SSE.
  r2.py           — S3 client with file:// filesystem-mirror fallback for dev.
  auth.py         — PBKDF2-SHA256 password hashing/verification helpers.
  login.py        — /login GET/POST, /logout, /login/guest, rate-limiting.
  session.py      — HMAC-signed session cookie mint/verify, auth middleware,
                    guest-mode sentinel (user_id=0, per-process secret).
  events.py       — Thread-safe SSE broadcaster (asyncio.Queue per client).
  db.py           — Two psycopg pools: viz_pool (claude_viz) and auth_pool
                    (read-only auth DB). Pools never join across DBs.
  cache.py        — In-process LRU with idle-time eviction for raw transcript
                    bytes (256 MB, 20-min idle).
  schema.sql      — Idempotent CREATE TABLE IF NOT EXISTS + safe
                    ALTER TABLE ... ADD COLUMN IF NOT EXISTS migrations.

public/           — Static assets served at /
  index.html      — Bootstraps React, Babel, JSZip from CDN; loads /src/*.
                    Backend rewrites this on every request to inject
                    window.BACKEND_URL, window.IS_GUEST, and mtime-based ?v=
                    cache-bust query strings on every static asset reference.
  app.css         — Dark-theme dashboard styles.

src/              — React JSX modules served at /src/* (in-browser Babel)
  app.jsx         — Top-level shell, routing, dashboard fetcher, SSE listener,
                    drag-drop file inspector, synthetic data preview.
  parser.js       — In-browser transcript parser used by the Inspector.
                    Pricing table here MUST match backend/pricing.py.
  dashboard-charts.jsx      — Core SVG panels (time series, HBar, burn rate).
  dashboard-charts-extra.jsx — Additional panels (context growth, cache TTL).
  context-growth-view.jsx    — Context growth visualisation components.
  detail-pane.jsx            — Session detail / inspector panes.
  event-helpers.jsx          — Shared event formatting helpers.
  synthetic-data.js          — Synthetic dashboard data generator.
  views/
    cache-view.jsx           — Cache analysis view.
    context-growth-view-v2.jsx — Updated context growth view.

scripts/          — scripts/plots/ccusage_plot.py: the upstream
                    nhz-io/ccusage-plot reference (visual-design parity).
                    Canonical analyst scripts (parse_session.py,
                    discord_mb.py) are NOT vendored here — invoke them by
                    absolute path under ~/.claude/scripts/.

tests/            — pytest suite
  conftest.py     — Injects repo root into sys.path; forces file-mode R2 and
                    test-safe env defaults (COOKIE_SECURE=0, etc.).
  test_parse.py   — Fixture-driven parser tests (see fixtures/parser/).
  test_api.py     — End-to-end API tests with fresh DB + mini R2 mirror.
  test_ingest.py  — Ingest pipeline tests (etag triggers, orphan deletion).
  test_auth.py    — PBKDF2 round-trip and known-vector tests.
  test_pricing.py — Rate lookup and cost computation tests.
  test_r2.py      — R2 client (S3 + file:// mode) tests.
  test_login.py   — Login flow tests.
  test_session.py — Session token mint/verify tests.

fixtures/         — Small JSONL + zip samples for parser and API tests.
  parser/         — Hand-crafted single-record samples, each under 1 KB.
  r2_mini/        — Mini filesystem mirror (2 projects, 4 sessions, 1 peer,
                    1 cross-session shared uuid) for ingest/API tests.

examples/         — Sample systemd service file (ccudash.service).

docs/             — Design docs and specs.
.claude/rules/    — Local doctrine (SV-PARSER-SPEC, SV-COST-SPLIT, etc.).
```

## Build and test commands

### Setup

```bash
# 1. Create the app database and apply schema
createdb claude_viz
psql claude_viz -f backend/schema.sql

# 2. Configure environment
cp backend/.env.example .env
# Edit .env to set real DATABASE_URL_VIZ, DATABASE_URL_AUTH, R2_*, ADMIN_TOKEN

# 3. Create virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

### Run the server

```bash
python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

The first request may block while the startup ingest runs (~30 s on a warm DB, several minutes for a cold cache against a large bucket). `/health` reflects ingest state via the `ingest_runs` table.

For local dev without R2 credentials, point `R2_ENDPOINT` at a filesystem mirror (e.g. `R2_ENDPOINT=file:///tmp/r2/`) — the R2 client falls back to walking the directory tree.

### Run tests

```bash
# Full suite (requires local PostgreSQL for test DB creation)
python3 -m pytest tests/ -q

# Individual modules
python3 -m pytest tests/test_parse.py -v
python3 -m pytest tests/test_api.py -v
python3 -m pytest tests/test_ingest.py -v
```

Tests use fixture-driven data, not real R2. `conftest.py` forces `R2_ENDPOINT=file:///tmp/sv-test-r2/` and sets `COOKIE_SECURE=0` so TestClient cookies work over plain HTTP.

### Manual operations

```bash
# Force an out-of-band ingest run
curl -X POST http://127.0.0.1:8000/admin/ingest \
  -H "X-Admin-Token: $ADMIN_TOKEN"

# Re-apply schema migrations (idempotent)
psql claude_viz -f backend/schema.sql
```

## Code style guidelines

- **Python**: `from __future__ import annotations` at the top of every `.py` file; type hints used throughout; no ORM — raw SQL via psycopg3.
- **JavaScript/JSX**: ES2020-ish, React functional components with hooks; globals attached to `window.` for cross-module sharing (e.g. `window.parseTranscript`, `window.rateForModel`).
- **SQL**: Parameterised queries only (`%s` placeholders); never interpolate user input into query strings. `DISTINCT ON (uuid)` for cross-file dedup at read time.
- **Naming**: `snake_case` for Python; `camelCase` for JS/JSX; SQL tables are singular nouns.
- **Error handling**: Parser silently skips malformed JSON lines (`orjson.JSONDecodeError` → `continue`). Ingest catches broad exceptions, logs to `ingest_runs.error`, and never crashes the scheduler.

## Testing instructions

- **Parser tests** (`test_parse.py`) are fixture-driven. Add a JSONL fixture to `fixtures/parser/` before changing parser behaviour, and map the test name 1:1 to the feature.
- **API tests** (`test_api.py`) spin up a fresh temporary DB + mini R2 mirror per fixture. They bypass auth by mounting only the `api.router` into a clean FastAPI app.
- **Ingest tests** (`test_ingest.py`) validate etag-based reparse triggers, orphan deletion, `turn_count` consistency, and cross-file uuid write-time retention (dedup is query-time).
- **Auth tests** (`test_auth.py`) verify PBKDF2 round-trips and constant-time comparison against garbage inputs.
- Keep fixture files small: `fixtures/parser/*.jsonl` under 1 KB each; `fixtures/r2_mini/` under a few KB. Larger samples go to `/tmp/analyst.BCYKic3p/r2/` (not committed).

## Security considerations

- **Auth**: PBKDF2-SHA256 with 200,000 iterations and per-user hex salts. Session cookies are HMAC-signed, `HttpOnly`, `Secure` (configurable via `COOKIE_SECURE`), `SameSite=strict`, 7-day TTL.
- **Guest mode**: `user_id=0` sessions are signed with a per-process secret regenerated at startup; cookies invalidate on restart. Guests are blocked from `/api/projects`, `/api/sessions*`, and `?project=` filter params.
- **Admin**: `POST /admin/ingest` requires `X-Admin-Token` header, checked via constant-time `hmac.compare_digest`. Admin paths also enforce origin/referer checks.
- **R2 file-mode path traversal**: `_safe_join` in `backend/r2.py` uses `os.path.realpath` to refuse keys that escape the bucket root (defence for sidecar `?path=../../../etc/passwd` attacks).
- **SQL injection**: All DB access uses parameterised psycopg3 queries.
- **No server-side parsing of user uploads**: The drag-drop inspector parses entirely in the browser. The backend only reads JSONLs from R2 (or its local mirror), never from HTTP uploads.

## Deployment process

Intended to run under systemd behind a reverse proxy. Key settings from `examples/ccudash.service`:

- `--timeout-graceful-shutdown 5` so SSE connections drain quickly.
- `TimeoutStopSec=10` for fast restarts.
- `Restart=always` with `RestartSec=5`.
- `After=network.target postgresql.service`.

```bash
# Typical systemd workflow
systemctl restart ccudash
systemctl status ccudash
journalctl -u ccudash -f
```

Schema migrations are idempotent — re-apply `backend/schema.sql` after any schema change. Bump `PARSER_VERSION` in `.env` whenever parser semantics or `pricing.py` rates change; every file reparses on the next ingest.

## Development conventions

- **Cost is always TTL-split**. `cache_creation` decomposes into `ephemeral_5m` (× 1.25 base) + `ephemeral_1h` (× 2 base). Tokens with no `ephemeral_*` split (legacy SDK) are charged at the 5m rate. Single-rate `cache_create` cost is banned.
- **Cross-file uuid dedup happens at READ time** via `DISTINCT ON (uuid)` in `/api/dashboard`, `/api/cache`, etc. Per-file `requestId` max-merge happens at INGEST time. There is no persisted Phase 2 rollup table.
- **Don't invoke `~/.claude/scripts/parse_session.py`** at runtime, and don't edit it from this repo. If the canonical Python and our port drift, fix it here, not there.
- **Tests use fixtures, not real R2.** The R2 client supports `R2_ENDPOINT=file:///path/to/mirror/` for offline dev.
- **Parser version invalidation:** Bump `PARSER_VERSION` in `.env` whenever parser semantics or `pricing.py` rates change — every file reparses on next ingest.
- **In-browser fallback retained:** The drag-drop FileReader path in `src/app.jsx` stays as an offline fallback. No upload endpoint exists.
