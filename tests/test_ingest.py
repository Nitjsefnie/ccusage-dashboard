import lzma
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from backend import db, ingest

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh_db(monkeypatch):
    """Per-test schema reset on a separate DB."""
    test_db = "claude_viz_test"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    if db._VIZ is not None:
        try:
            db._VIZ.close()
        except Exception:
            pass
    db._VIZ = None
    yield
    if db._VIZ is not None:
        try:
            db._VIZ.close()
        except Exception:
            pass
    db._VIZ = None
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.fixture
def mini_r2_env(monkeypatch):
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-ingest-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    yield Path(tmp) / "r2" / "claude"
    shutil.rmtree(tmp)


def test_ingest_inserts_one_row_per_jsonl(fresh_db, mini_r2_env):
    """Mini mirror has 5 jsonls (4 main + 1 peer) under 4 sessions
    in 2 projects. Expect 5 rows in `files`, 4 with is_main=true,
    4 distinct session_ids, 2 projects."""
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 5
    with db.viz_conn() as c:
        n = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert n == 5
        n_main = c.execute("SELECT COUNT(*) FROM files WHERE is_main").fetchone()[0]
        assert n_main == 4
        n_sess = c.execute("SELECT COUNT(DISTINCT session_id) FROM files").fetchone()[0]
        assert n_sess == 4
        n_proj = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        assert n_proj == 2


def test_records_populated_with_no_write_time_dedup(fresh_db, mini_r2_env):
    """sess-C main + sess-C agent + sess-D main all have uuid='shared-uuid-1'.
    The new ingest writes per-file with NO cross-file dedup at write time
    — so records has ALL three rows. Query-time DISTINCT ON is the dedup."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        assert n > 0
        # All three files' rows for 'shared-uuid-1' kept verbatim
        cnt = c.execute(
            "SELECT COUNT(*) FROM records WHERE uuid = 'shared-uuid-1'"
        ).fetchone()[0]
        assert cnt == 3
        # Query-time dedup gives 1
        cnt_distinct = c.execute(
            "SELECT COUNT(DISTINCT uuid) FROM records WHERE uuid = 'shared-uuid-1'"
        ).fetchone()[0]
        assert cnt_distinct == 1


def test_ctx_turns_stored_per_file(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, turn_count, jsonb_array_length(ctx_turns) FROM files"
        ).fetchall()
    for fk, tc, jlen in rows:
        assert tc == jlen, f"{fk}: turn_count={tc} but ctx_turns has {jlen}"


def test_etag_change_triggers_per_file_reparse(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before_etag = c.execute(
            "SELECT r2_etag FROM files WHERE file_key LIKE '%sess-A.jsonl'"
        ).fetchone()[0]
    target = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    target.write_text(target.read_text() + "\n")
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 1
    with db.viz_conn() as c:
        after_etag = c.execute(
            "SELECT r2_etag FROM files WHERE file_key LIKE '%sess-A.jsonl'"
        ).fetchone()[0]
    assert before_etag != after_etag


def test_parser_version_bump_reparses_all(fresh_db, mini_r2_env, monkeypatch):
    ingest.run_ingest(trigger="manual")
    monkeypatch.setenv("PARSER_VERSION", "2")
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 5  # all 5 files


def test_deleted_file_removed(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    target = mini_r2_env / "projA" / "sess-B" / "sess-B.jsonl"
    target.unlink()
    result = ingest.run_ingest(trigger="manual")
    assert result["deleted"] == 1
    with db.viz_conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM files WHERE file_key LIKE '%sess-B.jsonl'"
        ).fetchone()[0]
        assert n == 0


def test_records_cascade_on_file_delete(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    target = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    target.unlink()
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM records WHERE file_key LIKE '%sess-A.jsonl'"
        ).fetchone()[0]
        assert n == 0


def test_no_changes_second_run_is_zero_reparse(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    result2 = ingest.run_ingest(trigger="manual")
    assert result2["inserted"] == 0
    assert result2["reparsed"] == 0


def test_first_seen_at_uses_least(fresh_db, mini_r2_env):
    """projects.first_seen_at must NOT be locked at first-ingest mtime.
    Add a NEW file under an existing project with an earlier mtime;
    re-ingest must drag first_seen_at backward via LEAST(...) in ON CONFLICT."""
    import os as _os
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = c.execute(
            "SELECT first_seen_at FROM projects WHERE project_id = 'projA'"
        ).fetchone()[0]

    new_dir = mini_r2_env / "projA" / "sess-NEW"
    new_dir.mkdir()
    new_file = new_dir / "sess-NEW.jsonl"
    new_file.write_text(
        '{"type":"assistant","timestamp":"2026-05-07T09:00:00Z",'
        '"uuid":"u-new","requestId":"req-new","sessionId":"sess-NEW",'
        '"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        '"content":[{"type":"text","text":"x"}],'
        '"usage":{"input_tokens":1,"output_tokens":1,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    older_ts = before.timestamp() - 3600
    _os.utime(new_file, (older_ts, older_ts))

    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        after = c.execute(
            "SELECT first_seen_at FROM projects WHERE project_id = 'projA'"
        ).fetchone()[0]
    assert after < before, f"first_seen_at should move backward: was {before}, now {after}"


def test_xz_compressed_jsonl_ingests_transparently(fresh_db, mini_r2_env):
    """A `*.jsonl.xz` object ingests like its plain form: r2.get_object
    inflates it, the `.jsonl.xz` suffix is stripped for the stem so is_main
    still holds, and records populate. Here sess-A's main file is replaced
    by an xz copy — still 5 files, still 4 main, with records for sess-A."""
    plain = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    raw = plain.read_bytes()
    (plain.parent / "sess-A.jsonl.xz").write_bytes(lzma.compress(raw))
    plain.unlink()

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 5
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, is_main, session_id FROM files "
            "WHERE file_key LIKE '%sess-A.jsonl.xz'"
        ).fetchone()
        assert row is not None, "compressed file should ingest"
        assert row[0].endswith("sess-A/sess-A.jsonl.xz")
        assert row[1] is True, "stem after stripping .jsonl.xz == sess-A → is_main"
        assert row[2] == "sess-A"
        n_main = c.execute("SELECT COUNT(*) FROM files WHERE is_main").fetchone()[0]
        assert n_main == 4
        n_rec = c.execute(
            "SELECT COUNT(*) FROM records WHERE file_key LIKE '%sess-A.jsonl.xz'"
        ).fetchone()[0]
        assert n_rec > 0, "records populate from decompressed bytes"


def test_ingest_flushes_response_cache(fresh_db, mini_r2_env):
    from backend import cache

    cache.response_cache.put("stale-key", {"v": "old"})
    assert cache.response_cache.get("stale-key") == {"v": "old"}

    ingest.run_ingest(trigger="manual")

    assert cache.response_cache.get("stale-key") is None
