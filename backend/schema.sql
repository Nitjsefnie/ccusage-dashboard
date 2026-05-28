-- ccudash schema. Bump PARSER_VERSION env var to invalidate all rows.

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

CREATE INDEX IF NOT EXISTS records_uuid_idx ON records (uuid) WHERE uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_ts_idx ON records (ts);
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
