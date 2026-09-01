# claudit Doctrine

Local rules for the claudit repo. Global rules under `~/.claude/rules/**` still apply.

## Parser-spec ownership (SV-PARSER-SPEC)

The CANONICAL parser is `~/.claude/scripts/parse_session.py` (owned by
analyst). Both the in-browser `src/parser.js` AND the backend
`backend/parse.py` MIRROR that semantics. Keep all three in lockstep on:

- Per-file requestId `_merge_usage_max` during ingest (Phase 1,
  persisted into `records`)
- Cross-file UUID dedup, resolved at INGEST into `records.is_canonical`
  (see SV-CANONICAL-FLAG). The winner is still exactly what
  `DISTINCT ON (uuid) ORDER BY uuid, file_key` picked at read time.
  There is still no persisted Phase 2 rollup; the per-session SUM
  aggregation that used to live in `compute_cache` was dropped in R1.
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

## Backend is the ONLY load path (SV-NO-LOCAL-UPLOAD)

Supersedes SV-IN-BROWSER-FALLBACK, which required a drag-drop offline
fallback. That fallback was removed on 2026-07-21: there is now no way to
feed a transcript in from the browser — no drag-drop target, no
`FileReader`, no zip expansion, and no JSZip dependency. The only jsonls
the app reads come from R2, owned by the same operator.

There is still no upload endpoint and no server-side parsing of
operator-supplied jsonls. Do not reintroduce either, and do not add a
file picker as a "convenience" — an ingress path is exactly what was cut.

`src/parser.js` is NOT dead code and must stay. It still serves two live
consumers: `loadFromBackend()` parses the bytes from
`/api/sessions/{id}/transcript` client-side via `parseTranscript` +
`computeSessionStats`, and the Token Breakdown panel prices rows through
`window.rateForModel`. Its rate table remains bound to `backend/pricing.py`
by SV-PARSER-SPEC and the node parity test.

## Bundle distribution NOT applicable (SV-NO-BUNDLE)

claudit does not ship via `claude-setup.zip`. Distribution path is
git (this repo) + `pip install -r backend/requirements.txt`.

## Test fixtures stay small (SV-FIXTURE-SIZE)

`fixtures/parser/*.jsonl` are hand-crafted single-record samples,
each under 1 KB. `fixtures/r2_mini/` is the end-to-end mini mirror
(2 projects, 4 sessions, 1 sidecar, 1 cross-session shared uuid).
Don't grow either by accident — larger samples go under
`/tmp/analyst.BCYKic3p/r2/` (the local R2 mirror, not committed).

## Read-only on canonical paths (SV-READ-ONLY-CANONICAL)

claudit NEVER edits `~/.claude/scripts/parse_session.py` or
`~/.claude/scripts/discord_mb.py`. Those are owned by analyst, and per
global doctrine they are NOT copied, symlinked, or hardlinked into this
repo — invoke them by absolute path under `~/.claude/scripts/`.

## Schema fail-fast (SV-SCHEMA-FAIL-FAST)

`backend/db.schema_check()` runs at every server startup. It verifies
(a) `claudit.files` exists and (b) the auth DB's `users.config` is a
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

Cross-file uuid dedup is resolved at INGEST into `records.is_canonical`
(SV-CANONICAL-FLAG below). There is still NO persisted `record_uuids`
or `session_requests` table — both were dropped in R1 along with the
per-session rollup, the materialized hourly view, and the `sessions`
table. Reintroducing a persisted rollup or any cross-session table
requires a new migration, not a quiet code change.

## Dedup is a flag, not a read-time sort (SV-CANONICAL-FLAG)

`records.is_canonical` marks the row that
`DISTINCT ON (r.uuid) ORDER BY r.uuid, r.file_key` used to select at
read time. `line_num` breaks ties within a `file_key`, which the old
read-time ORDER BY left arbitrary. Rows with a NULL `uuid` are legacy
records kept verbatim (they were the `UNION ALL` leg) and are always
canonical.

Read endpoints MUST filter `WHERE is_canonical` and MUST NOT
reintroduce `DISTINCT ON (uuid)`. It was moved because `records` is
immutable between hourly ingests, yet every read re-sorted the whole
table to drop ~3.5% duplicates — `/api/cache` prefixed that dedup as a
CTE onto four queries and paid it four times per request (19.1s at
range=all; 0.52s after).

`ingest.recompute_canonical()` runs after EVERY successful ingest, not
only when files changed: adding or removing a FILE can change which row
wins for a uuid, and a freshly-migrated DB has the column defaulted to
TRUE across the board. The UPDATE only touches rows whose flag actually
flips, so a steady-state pass writes nothing. The column defaults to
TRUE so a migrated-but-not-yet-recomputed DB over-counts (behaves like
no dedup) rather than silently dropping rows.

Changing the winner rule means changing BOTH `recompute_canonical()`
and `src/parser.js`/`parse_session.py` semantics in lockstep
(SV-PARSER-SPEC).

## Foreign models are purged, not filtered (SV-SUPPRESSED-MODELS)

`suppressed_models(pattern, note, added_at)` lists models whose records
must not count. `ingest.purge_suppressed()` runs FIRST in
`_rebuild_derived_state()` — before the canonical pass — and DELETEs
matching `records` plus the `tool_uses` on the same lines.

Suppression is a delete, not a read-time predicate, because every read
path and every rollup already treats `records` as the truth; one deletion
keeps them consistent without ~15 extra filters a new endpoint could
forget. The `tool_uses` half is not optional: `tool_rollup` LEFT JOINs
`records` for the model, so a call whose record is gone reappears under a
NULL model.

Patterns are matched `model ILIKE pattern`, so `glm-%` covers a family
and a bare model id still matches exactly. The table ships EMPTY and must
stay that way in `schema.sql`: the same codebase is deployed over the
`zai` bucket as glmmeter, where `glm-%` would suppress everything.
Populate per deploy.

Why it exists: Claude Code writes every session under one tree whichever
endpoint served it, so resuming a session on the other lane interleaves
that provider's assistant entries into a transcript this bucket already
owns — real usage, but not ours, and priced against our table it invents
a cost. Removing a pattern brings the rows back only on a reparse (bump
`PARSER_VERSION`).

## Aggregates are precomputed at ingest (SV-ROLLUP)

`usage_rollup` holds pre-summed usage at grain
`(session_id, hour, model, is_main)`, rebuilt by
`ingest.rebuild_rollup()` after every successful ingest (AFTER
`recompute_canonical()` — it reads `is_canonical`). ~6.1k rows stand in
for ~286k records.

The grain is load-bearing, do not "simplify" it:

- **Keyed by HOUR, not by session.** A range filter sums only the
  in-range hours; a session-grained table would count a session
  straddling the boundary whole.
- **Carries MODEL.** A session's dominant model is
  `argmax(SUM(requests))` over the in-range rows — exactly what
  `MODE() WITHIN GROUP` computed from raw records, not an
  approximation.
- **Carries `first_ts`/`last_ts`.** Burn-rate span is
  `MAX(last_ts) - MIN(first_ts)`, which composes; a stored duration
  would not.

Only pure sums/counts/min/max may be served from it. **`PERCENTILE_CONT`
does not compose** — p50/p90 of a union of hours is not derivable from
per-hour p50/p90 — so `response_sizes` stays a live pass over `records`.
Do not "optimise" it onto the rollup.

The rollup is only valid for display buckets ≥ 1 hour. `/api/dashboard`
gates on `bucket_s >= 3600` and otherwise reads a live subquery shaped
with the same column names, so both paths share one set of queries. The
24h view buckets at 5 minutes and takes the live path.

It is derived state: anything that mutates `records` outside ingest must
rebuild or clear it, or reads serve a stale pre-aggregate. Totals are
verified equal to the equivalent `records` aggregate (requests, tokens,
cost, distinct sessions) — keep that true.

`records` cascades from `files`; `files` cascades from `projects`.
Reparse is idempotent: deleting a file's `records` rows and
re-inserting on the next ingest leaves the table byte-identical.

## Rates are a function of (model, timestamp) (SV-DATED-RATES)

`pricing.rate_for(model, ts)` — a model may carry dated overrides in
`DATED_RATES`, a list of `(end_exclusive_utc, rates)` windows per exact
model key. Cost must be computed against the timestamp of the request
being priced, never the time of rendering. `parse.py` passes each
record's own `ts`; omitting `ts` yields LIST price (conservative — never
silently applies a discount).

`DATED_RATES` is currently EMPTY and `RATE_EPOCHS` is `[]` — no live model
prices differently by date. The machinery stays regardless: the rule is
about the shape of pricing, not about whether a promotion happens to be
running. Tests drive it through the `synthetic_dated_rate` fixture in
`tests/conftest.py` rather than a live entry, so the path cannot rot while
the table is empty.

Any read path that RE-DERIVES rates from summed tokens must group by
`pricing.RATE_EPOCHS` (`api.rate_epoch_sql` / `api.fold_per_model`), or
its per-component breakdown drifts from the `SUM(cost_usd)` total it
claims to decompose. Totals themselves always come from the stored
per-record `cost_usd` — do not recompute them at read time.

## Model resolution flags estimates (SV-RATE-ESTIMATES)

`pricing.resolve()` returns `kind`: `exact` | `tier` | `default`.

- Model ids are normalised first: provider/region prefixes stripped
  (`us.anthropic.`, `anthropic/`) and `.` → `-`, so `claude-opus-4.8`
  resolves the same as `claude-opus-4-8`.
- An EXACT match allows only a dated-snapshot (`-20250514`) or bracket
  (`[1m]`) suffix after a table key. A short version suffix must NOT
  match a shorter key — billing `claude-opus-4-9` at `claude-opus-4`'s
  retired 15/75 is a silent 3x overcount.
- Unmatched Claude models fall back to their family's current-generation
  LIST rates and are flagged `tier`; anything else is `default`.
  Non-exact resolutions surface as `estimated_rate` in the API so a
  guessed figure is never presented as fact.

Never invent a rate for a variant we have no published price for
(e.g. `-fast`): let it fall back and be flagged.
