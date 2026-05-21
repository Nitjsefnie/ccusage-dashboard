# ccudash Doctrine

Local rules for the ccudash repo. Global rules under `~/.claude/rules/**` still apply.

## Parser-spec ownership (SV-PARSER-SPEC)

The CANONICAL parser is `~/.claude/scripts/parse_session.py` (owned by
analyst). Both the in-browser `src/parser.js` AND the backend
`backend/parse.py` MIRROR that semantics. Keep all three in lockstep on:

- Per-file requestId `_merge_usage_max` during ingest (Phase 1,
  persisted into `records`)
- Cross-file UUID dedup at READ time — `DISTINCT ON (uuid)` in the
  read endpoints (`backend/api.py`). There is no persisted Phase 2
  rollup; the per-session SUM aggregation that used to live in
  `compute_cache` was dropped in R1.
- `<task-notification>` ref detection for sub-agent jsonls
- Sidecar `data/subagents/agent-*.jsonl` resolution
- `MODEL_RATES` table (single source of truth: `backend/pricing.py`,
  initially copied from parse_session.py:1148-1166)

When you find a discrepancy: the Python canonical is right by default.
If the canonical itself has a real bug, file it for analyst via mailbox;
don't quietly fork the semantics here.

## Cost accounting is split TTL, always (SV-COST-SPLIT)

Every place this repo computes cost from `usage` records MUST split
`cache_creation` into `ephemeral_5m` and `ephemeral_1h` and apply the
correct multiplier:

- 5m write: 1.25× base input rate
- 1h write: 2× base input rate
- Tokens with no `ephemeral_*` split (legacy SDK records) charged at the
  5m rate (conservative undercount, not overcount).

Single-rate `cache_create` cost is BANNED. If you bump `MODEL_RATES`,
also bump `PARSER_VERSION` in `.env` so the next ingest reparses every
session.

## In-browser fallback retained (SV-IN-BROWSER-FALLBACK)

The PRIMARY load mode is now backend (`BACKEND_URL` set, app fetches from
`/api/dashboard`). The drag-drop FileReader path stays as an offline
fallback so an operator with a single jsonl on disk can inspect it
without standing up the backend. No upload endpoint, no server-side
parsing of operator-supplied jsonls (the only jsonls the backend reads
come from R2, owned by the same operator).

## Bundle distribution NOT applicable (SV-NO-BUNDLE)

ccudash does not ship via `claude-setup.zip`. Distribution path is
git (this repo) + `pip install -r backend/requirements.txt`.

## Test fixtures stay small (SV-FIXTURE-SIZE)

`fixtures/parser/*.jsonl` are hand-crafted single-record samples,
each under 1 KB. `fixtures/r2_mini/` is the end-to-end mini mirror
(2 projects, 4 sessions, 1 sidecar, 1 cross-session shared uuid).
Don't grow either by accident — larger samples go under
`/tmp/analyst.BCYKic3p/r2/` (the local R2 mirror, not committed).

## Read-only on canonical paths (SV-READ-ONLY-CANONICAL)

ccudash NEVER edits `~/.claude/scripts/parse_session.py` or
`~/.claude/scripts/discord_mb.py`. Those are owned by analyst, and per
global doctrine they are NOT copied, symlinked, or hardlinked into this
repo — invoke them by absolute path under `~/.claude/scripts/`.

## Schema fail-fast (SV-SCHEMA-FAIL-FAST)

`backend/db.schema_check()` runs at every server startup. It verifies
(a) `claude_viz.files` exists and (b) the auth DB's `users.config` is a
JSONB column. Either failure aborts startup with a clear error rather
than silently degrading to a broken auth flow at first login.

## Per-file files+records contract (SV-FILES-RECORDS)

The schema is per-file, not per-session. Two tables hold the parse
output (see `backend/schema.sql`):

- `files(file_key PK, project_id, session_id, is_main, r2_etag,
  r2_size_bytes, r2_last_modified, parsed_at, parser_version,
  ctx_turns JSONB, turn_count)` — one row per ingested JSONL, with
  the context-growth trace inlined as `ctx_turns`.
- `records(file_key, line_num, uuid, request_id, ts, model,
  fresh_tokens, cache_creation_tokens, cache_read_tokens,
  output_tokens, eph5_tokens, eph1h_tokens, cost_usd)`
  PK `(file_key, line_num)` — one row per usage-bearing line AFTER
  per-file Phase 1 max-merge for matching `request_id`.

Cross-file uuid dedup happens at READ time via `DISTINCT ON (uuid)`
in the read endpoints (`backend/api.py`). There is NO persisted
`record_uuids` or `session_requests` table — both were dropped in R1
along with the per-session rollup, the materialized hourly view, and
the `sessions` table. Reintroducing a persisted rollup or any
cross-session table requires a new migration, not a quiet code
change.

`records` cascades from `files`; `files` cascades from `projects`.
Reparse is idempotent: deleting a file's `records` rows and
re-inserting on the next ingest leaves the table byte-identical.
