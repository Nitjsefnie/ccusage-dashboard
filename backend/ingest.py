"""R2 → Postgres ingest.

Per-file granularity: every *.jsonl under bucket root → one row in `files`
+ N rows in `records` (per Phase-1-deduped record). Cross-file uuid
dedup is a query-time concern.

Reparse trigger per FILE: row missing OR etag changed OR parser_version
mismatch. Orphan files (R2 key gone) are deleted. CASCADE drops records.

Per-session work spans TWO transactions:
  1. DELETE FROM records WHERE file_key=... + INSERT INTO files (UPSERT)
     + bulk INSERT INTO records — all in one transaction so a crash
     mid-loop leaves either the old state or the new state.
  2. (Implicit) The orphan delete + projects upserts also commit in
     their own scopes; partial progress is fine because the sessions
     row's etag is only updated when the per-file txn lands.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from backend import db, events, parse, r2


def run_ingest(trigger: str) -> dict:
    started = datetime.now(timezone.utc)
    parser_version = os.environ.get("PARSER_VERSION", "1")

    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (started_at, trigger) VALUES (%s, %s) "
            "RETURNING id",
            (started, trigger),
        )
        run_id = cur.fetchone()[0]
        c.commit()

    listed = 0
    inserted = 0
    reparsed = 0
    deleted = 0
    err = None

    try:
        # Cache existing file_key → (etag, parser_version) for reparse decisions
        with db.viz_conn() as c:
            existing = {
                row[0]: (row[1], row[2])
                for row in c.execute(
                    "SELECT file_key, r2_etag, parser_version FROM files"
                ).fetchall()
            }

        seen_keys: set[str] = set()
        seen_projects: dict[str, dict] = {}

        for obj in r2.list_keys():
            if not obj.key.endswith(".jsonl"):
                continue
            parts = obj.key.split("/")
            if len(parts) < 3:
                continue
            project_id = parts[0]
            session_dir = parts[1]
            fname = parts[-1]
            stem = fname[: -len(".jsonl")]
            is_main = (stem == session_dir)
            session_id = session_dir
            listed += 1
            seen_keys.add(obj.key)

            proj = seen_projects.setdefault(project_id, {
                "project_id": project_id,
                "display_name": project_id,
                "first_seen_at": obj.last_modified,
                "last_seen_at": obj.last_modified,
            })
            if obj.last_modified < proj["first_seen_at"]:
                proj["first_seen_at"] = obj.last_modified
            if obj.last_modified > proj["last_seen_at"]:
                proj["last_seen_at"] = obj.last_modified

            stored = existing.get(obj.key)
            need_reparse = (
                stored is None
                or stored[0] != obj.etag
                or stored[1] != parser_version
            )
            if not need_reparse:
                continue

            blob = r2.get_object(obj.key)
            parsed = parse.parse_file(obj.key, blob)

            with db.viz_conn() as c, c.cursor() as cur:
                # Project upsert. first_seen_at uses LEAST so a later
                # ingest seeing an older file drags it backward.
                cur.execute(
                    "INSERT INTO projects (project_id, display_name, "
                    "first_seen_at, last_seen_at) "
                    "VALUES (%(project_id)s, %(display_name)s, "
                    "%(first_seen_at)s, %(last_seen_at)s) "
                    "ON CONFLICT (project_id) DO UPDATE SET "
                    "  display_name = EXCLUDED.display_name, "
                    "  first_seen_at = LEAST(projects.first_seen_at, "
                    "                        EXCLUDED.first_seen_at), "
                    "  last_seen_at = GREATEST(projects.last_seen_at, "
                    "                          EXCLUDED.last_seen_at)",
                    proj,
                )
                # Wipe existing records for this file (we use UPSERT on
                # files so records need an explicit DELETE before the
                # bulk INSERT below).
                cur.execute(
                    "DELETE FROM records WHERE file_key = %s", (obj.key,)
                )
                cur.execute(
                    """
                    INSERT INTO files (file_key, project_id, session_id,
                      is_main, r2_etag, r2_size_bytes, r2_last_modified,
                      parsed_at, parser_version, ctx_turns, turn_count,
                      rate_limit_hits)
                    VALUES (%(file_key)s, %(project_id)s, %(session_id)s,
                      %(is_main)s, %(r2_etag)s, %(r2_size_bytes)s,
                      %(r2_last_modified)s, %(parsed_at)s, %(parser_version)s,
                      %(ctx_turns)s::jsonb, %(turn_count)s,
                      %(rate_limit_hits)s::jsonb)
                    ON CONFLICT (file_key) DO UPDATE SET
                      project_id = EXCLUDED.project_id,
                      session_id = EXCLUDED.session_id,
                      is_main = EXCLUDED.is_main,
                      r2_etag = EXCLUDED.r2_etag,
                      r2_size_bytes = EXCLUDED.r2_size_bytes,
                      r2_last_modified = EXCLUDED.r2_last_modified,
                      parsed_at = EXCLUDED.parsed_at,
                      parser_version = EXCLUDED.parser_version,
                      ctx_turns = EXCLUDED.ctx_turns,
                      turn_count = EXCLUDED.turn_count,
                      rate_limit_hits = EXCLUDED.rate_limit_hits
                    """,
                    {
                        "file_key": obj.key,
                        "project_id": project_id,
                        "session_id": session_id,
                        "is_main": is_main,
                        "r2_etag": obj.etag,
                        "r2_size_bytes": obj.size,
                        "r2_last_modified": obj.last_modified,
                        "parsed_at": datetime.now(timezone.utc),
                        "parser_version": parser_version,
                        "ctx_turns": json.dumps(parsed["ctx_turns"], default=str),
                        "turn_count": parsed["turn_count"],
                        "rate_limit_hits": json.dumps(
                            parsed.get("rate_limit_hits", []), default=str
                        ),
                    },
                )
                # tool_uses cascades from files; explicit DELETE so a
                # reparse doesn't leave stale rows behind.
                cur.execute(
                    "DELETE FROM tool_uses WHERE file_key = %s", (obj.key,)
                )
                if parsed.get("tool_uses"):
                    cur.executemany(
                        """
                        INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name)
                        VALUES (%(file_key)s, %(line_num)s, %(idx)s, %(ts)s, %(tool_name)s)
                        """,
                        parsed["tool_uses"],
                    )
                if parsed["records"]:
                    cur.executemany(
                        """
                        INSERT INTO records (file_key, line_num, uuid,
                          request_id, ts, model, fresh_tokens,
                          cache_creation_tokens, cache_read_tokens,
                          output_tokens, eph5_tokens, eph1h_tokens, cost_usd,
                          text_chars)
                        VALUES (%(file_key)s, %(line_num)s, %(uuid)s,
                          %(request_id)s, %(ts)s, %(model)s,
                          %(fresh_tokens)s, %(cache_creation_tokens)s,
                          %(cache_read_tokens)s, %(output_tokens)s,
                          %(eph5_tokens)s, %(eph1h_tokens)s, %(cost_usd)s,
                          %(text_chars)s)
                        """,
                        parsed["records"],
                    )
                c.commit()
            if stored is None:
                inserted += 1
            else:
                reparsed += 1

        # Orphan files
        with db.viz_conn() as c, c.cursor() as cur:
            if seen_keys:
                cur.execute(
                    "DELETE FROM files WHERE file_key != ALL(%s) RETURNING 1",
                    (list(seen_keys),),
                )
            else:
                cur.execute("DELETE FROM files RETURNING 1")
            deleted = len(cur.fetchall())
            c.commit()

    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    finished = datetime.now(timezone.utc)
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at=%s, r2_listed=%s, "
            "reparsed=%s, inserted=%s, deleted=%s, error=%s WHERE id=%s",
            (finished, listed, reparsed, inserted, deleted, err, run_id),
        )
        c.commit()

    summary = {
        "id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "trigger": trigger,
        "r2_listed": listed,
        "inserted": inserted,
        "reparsed": reparsed,
        "deleted": deleted,
        "error": err,
    }
    # Notify connected SSE clients so the dashboard re-fetches without
    # a page reload. Threadsafe: ingest may run in a scheduler thread.
    if err is None and (inserted or reparsed or deleted):
        events.broadcast_threadsafe("ingest_done", summary)
    return summary
