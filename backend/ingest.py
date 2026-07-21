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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from backend import cache, db, events, parse, r2


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
        # (obj, proj, project_id, session_id, is_main, stored) per file
        # needing work; fetched+parsed below on a pool.
        todo: list[tuple] = []

        for obj in r2.list_keys():
            # Objects may be stored plain or per-object xz-compressed; r2
            # get_object/get_stream inflate `.xz` transparently. Strip the
            # matched suffix so the stem (and thus is_main) is unaffected by
            # compression.
            if obj.key.endswith(".jsonl.xz"):
                suffix_len = len(".jsonl.xz")
            elif obj.key.endswith(".jsonl"):
                suffix_len = len(".jsonl")
            else:
                continue
            parts = obj.key.split("/")
            if len(parts) < 3:
                continue
            project_id = parts[0]
            session_dir = parts[1]
            fname = parts[-1]
            stem = fname[:-suffix_len]
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

            todo.append((obj, proj, project_id, session_id, is_main, stored))

        # Fetch + parse is ~88% of per-file wall time and is network-bound
        # (one R2 GET each), so it runs on a thread pool. Persistence stays
        # on this thread: the per-file transaction boundary, and therefore
        # ordering and failure semantics, are exactly as before. Work is
        # submitted in bounded chunks so an 8k-file reparse does not hold
        # every inflated blob in memory at once.
        workers = _worker_count()
        chunk = max(1, workers * 4)
        for start in range(0, len(todo), chunk):
            batch = todo[start:start + chunk]
            if workers == 1:
                results = ((item, _fetch_and_parse(item[0].key)) for item in batch)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(_fetch_and_parse, item[0].key): item
                        for item in batch
                    }
                    results = [
                        (futures[f], f.result()) for f in as_completed(futures)
                    ]
            for (obj, proj, project_id, session_id, is_main, stored), parsed in results:
                _persist(
                    obj, proj, project_id, session_id, is_main,
                    parsed, parser_version,
                )
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
    # Data changed: invalidate the response cache, then notify connected
    # SSE clients so the dashboard re-fetches without a page reload. The
    # flush runs BEFORE the broadcast so a client reacting to ingest_done
    # cannot read a stale entry. Threadsafe: ingest may run in a scheduler
    # thread.
    if err is None and (inserted or reparsed or deleted):
        cache.response_cache.clear()
        events.broadcast_threadsafe("ingest_done", summary)
    return summary



def _worker_count() -> int:
    """Fetch+parse concurrency.

    Unset or unparseable -> auto (network-bound work, so oversubscribe
    cores). An explicit number is honoured, clamped to at least 1, so
    INGEST_WORKERS=1 is a real "go sequential" switch for debugging.
    """
    auto = min(16, (os.cpu_count() or 4) * 2)
    raw = os.environ.get("INGEST_WORKERS", "").strip()
    if not raw:
        return auto
    try:
        return max(1, int(raw))
    except ValueError:
        return auto


def _fetch_and_parse(key: str) -> dict:
    """Runs on a pool thread. Touches no DB connection."""
    return parse.parse_file(key, r2.get_object(key))


def _persist(obj, proj, project_id, session_id, is_main, parsed,
             parser_version) -> None:
    """One file, one transaction — identical to the pre-pool behaviour."""
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
              prompt_count, rate_limit_hits)
            VALUES (%(file_key)s, %(project_id)s, %(session_id)s,
              %(is_main)s, %(r2_etag)s, %(r2_size_bytes)s,
              %(r2_last_modified)s, %(parsed_at)s, %(parser_version)s,
              %(ctx_turns)s::jsonb, %(turn_count)s,
              %(prompt_count)s, %(rate_limit_hits)s::jsonb)
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
              prompt_count = EXCLUDED.prompt_count,
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
                "prompt_count": parsed["prompt_count"],
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
                INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, is_error)
                VALUES (%(file_key)s, %(line_num)s, %(idx)s, %(ts)s, %(tool_name)s, %(is_error)s)
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
                  text_chars, reply_latency_s)
                VALUES (%(file_key)s, %(line_num)s, %(uuid)s,
                  %(request_id)s, %(ts)s, %(model)s,
                  %(fresh_tokens)s, %(cache_creation_tokens)s,
                  %(cache_read_tokens)s, %(output_tokens)s,
                  %(eph5_tokens)s, %(eph1h_tokens)s, %(cost_usd)s,
                  %(text_chars)s, %(reply_latency_s)s)
                """,
                parsed["records"],
            )
        c.commit()