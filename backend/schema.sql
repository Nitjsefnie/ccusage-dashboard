-- claudit schema. Bump PARSER_VERSION env var to invalidate all rows.

CREATE TABLE IF NOT EXISTS projects (
  project_id    TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  file_key            TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  session_id          TEXT NOT NULL,
  is_main             BOOLEAN NOT NULL,
  r2_etag             TEXT NOT NULL,
  r2_size_bytes       BIGINT NOT NULL,
  r2_last_modified    TIMESTAMPTZ NOT NULL,
  parsed_at           TIMESTAMPTZ NOT NULL,
  parser_version      TEXT NOT NULL,
  ctx_turns           JSONB NOT NULL DEFAULT '[]'::jsonb,
  turn_count          INT   NOT NULL DEFAULT 0,
  prompt_count        INT   NOT NULL DEFAULT 0,
  rate_limit_hits     JSONB NOT NULL DEFAULT '[]'::jsonb
);

ALTER TABLE files ADD COLUMN IF NOT EXISTS
  rate_limit_hits JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE files ADD COLUMN IF NOT EXISTS
  prompt_count INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS files_project_idx ON files (project_id);
CREATE INDEX IF NOT EXISTS files_session_idx ON files (session_id);
CREATE INDEX IF NOT EXISTS files_modified_idx ON files (r2_last_modified);

CREATE TABLE IF NOT EXISTS records (
  file_key                TEXT NOT NULL REFERENCES files(file_key) ON DELETE CASCADE,
  line_num                INT  NOT NULL,
  uuid                    TEXT,
  request_id              TEXT NOT NULL DEFAULT '',
  ts                      TIMESTAMPTZ,
  model                   TEXT NOT NULL,
  fresh_tokens            BIGINT NOT NULL DEFAULT 0,
  cache_creation_tokens   BIGINT NOT NULL DEFAULT 0,
  cache_read_tokens       BIGINT NOT NULL DEFAULT 0,
  output_tokens           BIGINT NOT NULL DEFAULT 0,
  eph5_tokens             BIGINT NOT NULL DEFAULT 0,
  eph1h_tokens            BIGINT NOT NULL DEFAULT 0,
  cost_usd                NUMERIC(12,6) NOT NULL DEFAULT 0,
  text_chars              BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (file_key, line_num)
);

-- Idempotent migration for existing DBs.
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  text_chars BIGINT NOT NULL DEFAULT 0;
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  reply_latency_s NUMERIC(10,3);

-- Cross-file uuid dedup, resolved at INGEST instead of on every read.
-- `records` is immutable between ingests, but every read endpoint was
-- re-running DISTINCT ON (uuid) over the whole table -- a 296k-row sort
-- to drop ~3.5% duplicates, per request, per range, per project. This
-- flag marks the row that DISTINCT ON (uuid) ORDER BY uuid, file_key,
-- line_num would have kept; readers filter on it instead of sorting.
-- Recomputed by ingest.recompute_canonical() after every ingest, since
-- adding or removing a FILE can change which row wins for a uuid.
--
-- Defaults to TRUE so a freshly-migrated DB over-counts nothing before
-- the first recompute -- it behaves exactly like the pre-dedup state
-- (every row kept) rather than silently dropping rows.
ALTER TABLE records ADD COLUMN IF NOT EXISTS
  is_canonical BOOLEAN NOT NULL DEFAULT TRUE;

-- Pre-aggregated usage, rebuilt at ingest (ingest.rebuild_rollup).
--
-- `records` only changes when an ingest runs, but every dashboard request
-- was re-deriving the entire history from raw rows: five separate full
-- aggregate passes over ~285k canonical records, per request, per range,
-- per project. This holds the same numbers already summed.
--
-- Grain is (session_id, hour, model) deliberately:
--   * keyed by HOUR, not by session, so a range filter sums only the
--     in-range hours -- a session straddling the boundary would otherwise
--     be counted whole. Sums, counts and min/max ts all compose exactly.
--   * carrying MODEL means a session's dominant model is argmax(requests)
--     over the in-range rows -- exact, not an approximation of MODE().
-- What does NOT compose is PERCENTILE_CONT, so response-size p50/p90 is
-- still computed live from records (see /api/dashboard).
CREATE TABLE IF NOT EXISTS usage_rollup (
  session_id          TEXT        NOT NULL,
  project_id          TEXT        NOT NULL,
  hour                TIMESTAMPTZ NOT NULL,
  model               TEXT        NOT NULL,
  is_main             BOOLEAN     NOT NULL DEFAULT TRUE,
  first_ts            TIMESTAMPTZ NOT NULL,
  last_ts             TIMESTAMPTZ NOT NULL,
  requests            BIGINT      NOT NULL DEFAULT 0,
  fresh_tokens        BIGINT      NOT NULL DEFAULT 0,
  output_tokens       BIGINT      NOT NULL DEFAULT 0,
  cache_creation_tokens BIGINT    NOT NULL DEFAULT 0,
  cache_read_tokens   BIGINT      NOT NULL DEFAULT 0,
  eph5_tokens         BIGINT      NOT NULL DEFAULT 0,
  eph1h_tokens        BIGINT      NOT NULL DEFAULT 0,
  cost_usd            NUMERIC(18,8) NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, hour, model, is_main)
);
CREATE INDEX IF NOT EXISTS usage_rollup_hour_idx ON usage_rollup (hour);
CREATE INDEX IF NOT EXISTS usage_rollup_project_idx ON usage_rollup (project_id, hour);

-- Pre-aggregated tool calls, rebuilt at ingest alongside usage_rollup.
-- Serves /api/tool-usage, /api/tool-error-rate, and the dashboard line-churn
-- series without scanning raw tool_uses on hourly-or-coarser views.
--
-- The two endpoints count DIFFERENT populations, so both are stored:
--   n_total  every tool_use in the group           -> /api/tool-usage
--   n_rated  those with is_error NOT NULL AND a matching records row
--            (tool_error_rate INNER JOINs records) -> denominator
--   n_error  of those, the ones that errored       -> numerator
--   lines_*  additive Edit/Write churn             -> /api/dashboard
-- `model` is '' when no records row matched; tool_error_rate excludes
-- those, which is what its inner join did.
CREATE TABLE IF NOT EXISTS tool_rollup (
  hour        TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  model       TEXT        NOT NULL,
  tool_name   TEXT        NOT NULL,
  n_total     BIGINT      NOT NULL DEFAULT 0,
  n_rated     BIGINT      NOT NULL DEFAULT 0,
  n_error     BIGINT      NOT NULL DEFAULT 0,
  lines_added BIGINT      NOT NULL DEFAULT 0,
  lines_deleted BIGINT    NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, project_id, model, tool_name)
);
ALTER TABLE tool_rollup ADD COLUMN IF NOT EXISTS
  lines_added BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tool_rollup ADD COLUMN IF NOT EXISTS
  lines_deleted BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS tool_rollup_hour_idx ON tool_rollup (hour);
CREATE INDEX IF NOT EXISTS tool_rollup_project_idx ON tool_rollup (project_id, hour);

-- Pre-aggregated reply-latency bands + outlier dots for /api/reply-latency.
--
-- Percentiles do NOT compose across buckets, so unlike usage_rollup this
-- cannot be stored at one fine grain and summed up. What makes it
-- precomputable anyway is that the display buckets are epoch-aligned and
-- deterministic — floor(epoch / bucket_s) * bucket_s does not move with
-- `now` — and the UI only ever uses five widths (300/3600/21600/43200/
-- 86400). So the percentiles are computed once PER bucket_s and a range
-- filter merely selects which buckets to return. Exact, not approximate.
-- 300s (the 24h view) is excluded: it would need a row per 5 minutes of
-- all history to serve one day, so that range keeps the live path.
--
-- project_id = '' is the ALL-PROJECTS row. It has to be stored
-- separately because a filter changes the population inside each
-- (bucket, model) group, and p50 over all projects is not derivable from
-- the per-project p50s. The model filter needs no such treatment — the
-- rows are already grouped by model, so filtering selects whole groups.
--
-- `outliers` holds the top/bottom 1% dots the panel draws, as
-- [{ts, latency_s, file_key, line_num, kind}], only for buckets with
-- n >= 100 (1% of fewer would just be the min/max).
CREATE TABLE IF NOT EXISTS latency_rollup (
  bucket_s    INTEGER     NOT NULL,
  bucket      TIMESTAMPTZ NOT NULL,
  project_id  TEXT        NOT NULL,
  model       TEXT        NOT NULL,
  n           BIGINT      NOT NULL,
  p10         DOUBLE PRECISION,
  p50         DOUBLE PRECISION,
  p90         DOUBLE PRECISION,
  outliers    JSONB       NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (bucket_s, bucket, project_id, model)
);
CREATE INDEX IF NOT EXISTS latency_rollup_lookup_idx
  ON latency_rollup (bucket_s, project_id, bucket);

-- Only ~19% of records carry a reply_latency_s; a partial covering index
-- keeps the live path (and the rollup build) off the other 81%.
CREATE INDEX IF NOT EXISTS records_latency_idx ON records (ts)
  INCLUDE (model, reply_latency_s, file_key, line_num)
  WHERE reply_latency_s IS NOT NULL;

CREATE INDEX IF NOT EXISTS records_uuid_idx ON records (uuid) WHERE uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_ts_idx ON records (ts);
-- Every read endpoint filters `is_canonical AND ts >= ...`.
CREATE INDEX IF NOT EXISTS records_canonical_ts_idx
  ON records (ts) WHERE is_canonical;
CREATE INDEX IF NOT EXISTS records_model_idx ON records (model);
CREATE INDEX IF NOT EXISTS records_request_idx ON records (request_id) WHERE request_id <> '';

CREATE TABLE IF NOT EXISTS tool_uses (
  file_key   TEXT NOT NULL REFERENCES files(file_key) ON DELETE CASCADE,
  line_num   INT  NOT NULL,
  idx        INT  NOT NULL,            -- index within the assistant msg's content[]
  ts         TIMESTAMPTZ,
  tool_name  TEXT NOT NULL,
  PRIMARY KEY (file_key, line_num, idx)
);

CREATE INDEX IF NOT EXISTS tool_uses_ts_idx   ON tool_uses (ts);
CREATE INDEX IF NOT EXISTS tool_uses_tool_idx ON tool_uses (tool_name);

-- 2026-05-08: tool_uses.is_error filled at parse time by matching
-- assistant tool_use.id to user tool_result.tool_use_id within the
-- same JSONL file. NULL = unmatched (no later tool_result block in
-- the file). Read endpoints WHERE is_error IS NOT NULL to compute
-- the error rate over settled calls only.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS is_error BOOLEAN;

-- 2026-07-29 (issue #10): per-call line churn for edit/write tools,
-- derived from the tool CALL arguments at parse time (see
-- parse._tool_churn). Two separate POSITIVE series — deletions are
-- not negative additions. Errored calls are zeroed at parse. Both
-- default 0, so historical rows read as "no churn known" until a
-- PARSER_VERSION bump reparses them.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS
  lines_added BIGINT NOT NULL DEFAULT 0;
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS
  lines_deleted BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS ingest_runs (
  id              BIGSERIAL PRIMARY KEY,
  started_at      TIMESTAMPTZ NOT NULL,
  finished_at     TIMESTAMPTZ,
  trigger         TEXT NOT NULL,
  r2_listed       INT,
  reparsed        INT,
  inserted        INT,
  deleted         INT,
  error           TEXT
);

-- 2026-09-01: models whose records must not count toward any panel.
--
-- Claude Code writes every session under ~/.claude/projects/ regardless
-- of which endpoint served it, so resuming a session on the other lane
-- (Anthropic vs the z.ai GLM endpoint) interleaves that provider's
-- assistant entries into a transcript the archiver has already filed
-- under this deploy's bucket. The rows are real usage, just not OUR
-- usage: pricing them against the Anthropic table invents a cost.
--
-- Rows are patterns matched with `model ILIKE pattern`, so one
-- 'glm-%' covers a whole family and a bare model id still matches
-- exactly. Ships EMPTY on purpose -- this same codebase is deployed
-- over the zai bucket as glmmeter, where 'glm-%' would suppress
-- everything. Populate per deploy:
--
--   INSERT INTO suppressed_models (pattern, note)
--        VALUES ('glm-%', 'z.ai lane; see glmmeter');
--
-- Enforced by ingest.purge_suppressed(), which runs before the
-- canonical pass on every ingest: matching `records` and their
-- `tool_uses` are deleted, so every read path and rollup excludes
-- them without a filter of its own. Removing a pattern brings the
-- rows back only on a reparse (bump PARSER_VERSION).
CREATE TABLE IF NOT EXISTS suppressed_models (
  pattern   TEXT PRIMARY KEY,
  note      TEXT,
  added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
