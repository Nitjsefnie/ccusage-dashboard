import inspect
import lzma
import os
import shutil
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from backend import api, cache, db, ingest
from backend.api_dashboard import dashboard

_REPO_ROOT = Path(__file__).resolve().parent.parent

# One of the five jsonl keys in fixtures/r2_mini, used as the object whose
# fetch is made to fail.
_FLAKY_KEY = "projA/sess-A/sess-A.jsonl"


def _scalar(cur, sql: str, params=None):
    """First column of the first row; the query must yield one."""
    row = (cur.execute(sql, params) if params is not None
           else cur.execute(sql)).fetchone()
    assert row is not None, f"expected a row: {sql[:80]}"
    return row[0]


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB."""
    test_db = "claudit_test"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.reset_viz_pool()
    yield
    db.reset_viz_pool()
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.fixture(name="mini_r2_env")
def _mini_r2_env_fixture(monkeypatch):
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
        n = _scalar(c, "SELECT COUNT(*) FROM files")
        assert n == 5
        n_main = _scalar(c, "SELECT COUNT(*) FROM files WHERE is_main")
        assert n_main == 4
        n_sess = _scalar(c, "SELECT COUNT(DISTINCT session_id) FROM files")
        assert n_sess == 4
        n_proj = _scalar(c, "SELECT COUNT(*) FROM projects")
        assert n_proj == 2


def test_records_populated_with_no_write_time_dedup(fresh_db, mini_r2_env):
    """sess-C main + sess-C agent + sess-D main all have uuid='shared-uuid-1'.
    The new ingest writes per-file with NO cross-file dedup at write time
    — so records has ALL three rows. Query-time DISTINCT ON is the dedup."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM records")
        assert n > 0
        # All three files' rows for 'shared-uuid-1' kept verbatim
        cnt = _scalar(c,
            "SELECT COUNT(*) FROM records WHERE uuid = 'shared-uuid-1'"
        )
        assert cnt == 3
        # Query-time dedup gives 1
        cnt_distinct = _scalar(c,
            "SELECT COUNT(DISTINCT uuid) FROM records WHERE uuid = 'shared-uuid-1'"
        )
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
        before_etag = _scalar(c,
            "SELECT r2_etag FROM files WHERE file_key LIKE '%sess-A.jsonl'"
        )
    target = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    target.write_text(target.read_text() + "\n")
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 1
    with db.viz_conn() as c:
        after_etag = _scalar(c,
            "SELECT r2_etag FROM files WHERE file_key LIKE '%sess-A.jsonl'"
        )
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
        n = _scalar(c,
            "SELECT COUNT(*) FROM files WHERE file_key LIKE '%sess-B.jsonl'"
        )
        assert n == 0


def test_records_cascade_on_file_delete(fresh_db, mini_r2_env):
    ingest.run_ingest(trigger="manual")
    target = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    target.unlink()
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        n = _scalar(c,
            "SELECT COUNT(*) FROM records WHERE file_key LIKE '%sess-A.jsonl'"
        )
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
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = _scalar(c,
            "SELECT first_seen_at FROM projects WHERE project_id = 'projA'"
        )

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
    os.utime(new_file, (older_ts, older_ts))

    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        after = _scalar(c,
            "SELECT first_seen_at FROM projects WHERE project_id = 'projA'"
        )
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
        n_main = _scalar(c, "SELECT COUNT(*) FROM files WHERE is_main")
        assert n_main == 4
        n_rec = _scalar(c,
            "SELECT COUNT(*) FROM records WHERE file_key LIKE '%sess-A.jsonl.xz'"
        )
        assert n_rec > 0, "records populate from decompressed bytes"


def test_is_canonical_matches_read_time_distinct_on(fresh_db, mini_r2_env):
    """The ingest-time flag must select exactly the rows the old read-time
    `DISTINCT ON (uuid) ORDER BY uuid, file_key` would have kept.

    This is the invariant that lets the read endpoints filter a boolean
    instead of re-sorting the whole table (SV-CANONICAL-FLAG). The mini
    mirror carries a cross-session shared uuid, so there is a real
    duplicate to resolve.
    """
    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        flagged = c.execute(
            "SELECT file_key, line_num FROM records "
            "WHERE is_canonical ORDER BY file_key, line_num"
        ).fetchall()
        # What the read endpoints used to compute on every request.
        expected = c.execute(
            """
            SELECT file_key, line_num FROM (
              (SELECT DISTINCT ON (uuid) file_key, line_num
                 FROM records WHERE uuid IS NOT NULL
                ORDER BY uuid, file_key, line_num)
              UNION ALL
              (SELECT file_key, line_num FROM records WHERE uuid IS NULL)
            ) t ORDER BY file_key, line_num
            """
        ).fetchall()
        dupes = c.execute(
            "SELECT COUNT(*) FROM records WHERE NOT is_canonical"
        ).fetchone()

    assert flagged == expected
    assert dupes is not None and dupes[0] > 0, (
        "fixture must contain a cross-file duplicate, or this proves nothing"
    )


def test_recompute_canonical_is_idempotent(fresh_db, mini_r2_env):
    """A steady-state pass must not rewrite rows — it runs after every
    ingest, including no-op ones."""
    ingest.run_ingest(trigger="manual")
    assert ingest.recompute_canonical() == 0


def test_warm_common_covers_every_warmed_range(fresh_db, mini_r2_env, monkeypatch):
    """Every endpoint warm_common touches must be warmed for EVERY range
    it claims to cover — a warm keyed on something the UI never requests
    is dead work and leaves the real key cold.

    Regression: /api/projects gained a `range` parameter, but warm_common
    still called cache.warm(api.list_projects) bare. cache.warm falls back
    to the endpoint's signature default ("30d") while the UI opens on
    "all", so the one request every page load makes was never warmed.
    """
    monkeypatch.setenv("CLAUDIT_WARM_CACHE", "1")
    ingest.run_ingest(trigger="manual")

    warmed = (
        dashboard, api.activity_heatmap, api.tool_usage,
        api.tool_error_rate, api.reply_latency, api.list_projects,
    )
    # The warms run on a background pool; give them a bounded moment.
    deadline = time.time() + 60
    missing = None
    while time.time() < deadline:
        missing = [
            f"{fn.__qualname__}(range={rng})"
            for rng in ingest.WARM_RANGES
            for fn in warmed
            if cache.response_cache.get(_warm_key(fn, rng)) is None
        ]
        if not missing:
            break
        time.sleep(0.25)

    assert not missing, "warm_common left these uncached: " + ", ".join(missing)


def _warm_key(fn, rng: str) -> str:
    """Reproduce cache_response's key for a request at `rng`.

    Built from the endpoint's own signature so it stays correct as params
    are added — which is exactly what broke /api/projects.
    """
    target = getattr(fn, "__wrapped__", fn)
    kwargs = {}
    for name, param in inspect.signature(target).parameters.items():
        default = param.default
        kwargs[name] = getattr(default, "default", default)
    kwargs["rng"] = rng
    if "fresh" in kwargs:
        kwargs["fresh"] = 0
    return target.__qualname__ + ":" + repr(sorted(kwargs.items()))


def test_ingest_marks_response_cache_stale(fresh_db, mini_r2_env):
    """Ingest marks cached responses stale but leaves them SERVABLE.

    It used to clear() the cache, which dropped every reader onto the
    uncached path once an hour — 8s+ for /api/dashboard at range=all.
    Stale-while-revalidate keeps the previous numbers available while the
    refresh runs off the request path.
    """
    cache.response_cache.put("stale-key", {"v": "old"})
    entry = cache.response_cache.get_entry("stale-key")
    assert entry == ({"v": "old"}, False), "fresh before ingest"

    ingest.run_ingest(trigger="manual")

    entry = cache.response_cache.get_entry("stale-key")
    assert entry is not None, "ingest must NOT drop the entry"
    value, is_stale = entry
    assert value == {"v": "old"}, "previous response still servable"
    assert is_stale is True, "and flagged for background refresh"


def _snapshot():
    """Full ingest output, ordered so it is comparable across runs."""
    with db.viz_conn() as c:
        files = c.execute(
            "SELECT file_key, project_id, session_id, is_main, r2_etag, "
            "turn_count, prompt_count, parser_version FROM files "
            "ORDER BY file_key"
        ).fetchall()
        records = c.execute(
            "SELECT file_key, line_num, uuid, request_id, model, fresh_tokens, "
            "cache_creation_tokens, cache_read_tokens, output_tokens, "
            "eph5_tokens, eph1h_tokens, cost_usd FROM records "
            "ORDER BY file_key, line_num"
        ).fetchall()
        tools = c.execute(
            "SELECT file_key, line_num, idx, tool_name, is_error FROM tool_uses "
            "ORDER BY file_key, line_num, idx"
        ).fetchall()
        projects = c.execute(
            "SELECT project_id, first_seen_at, last_seen_at FROM projects "
            "ORDER BY project_id"
        ).fetchall()
    return files, records, tools, projects


def test_parallel_ingest_matches_sequential_exactly(
    fresh_db, mini_r2_env, monkeypatch
):
    """Concurrency must not change what lands in the DB.

    Fetch+parse is parallelised; if that leaked into ordering, dedup, or
    the per-file transaction boundary, the two snapshots would diverge.
    """
    monkeypatch.setenv("INGEST_WORKERS", "1")
    ingest.run_ingest("test-seq")
    sequential = _snapshot()

    # Wipe and re-ingest the identical mirror with a pool.
    with db.viz_conn() as c:
        c.execute("DELETE FROM files")
        c.execute("DELETE FROM projects")
        c.commit()

    monkeypatch.setenv("INGEST_WORKERS", "8")
    ingest.run_ingest("test-par")
    parallel = _snapshot()

    assert parallel[0] == sequential[0], "files differ"
    assert parallel[1] == sequential[1], "records differ"
    assert parallel[2] == sequential[2], "tool_uses differ"
    assert parallel[3] == sequential[3], "projects differ"
    assert len(sequential[1]) > 0, "fixture produced no records — vacuous test"


def test_ingest_workers_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("INGEST_WORKERS", raising=False)
    assert ingest.worker_count() >= 1
    monkeypatch.setenv("INGEST_WORKERS", "0")
    assert ingest.worker_count() == 1
    monkeypatch.setenv("INGEST_WORKERS", "not-a-number")
    assert ingest.worker_count() >= 1


# ------------------------------------------- per-object R2 failures (#2)

def _patch_fetch(monkeypatch, key, fail_times, exc=None):
    """Make r2.get_object fail for `key` on its first `fail_times` calls.

    `exc` is the exception to raise, defaulting to a plain OSError.
    Returns (call_counts, slept) — the per-key GET count (the pooled path
    calls this from several threads, hence the lock) and the backoff sleeps
    the retry asked for, which are swallowed so the suite does not pay them.
    """
    real_get = ingest.r2.get_object
    counts: Counter = Counter()
    slept: list[float] = []
    lock = threading.Lock()

    def flaky(k):
        with lock:
            counts[k] += 1
            n = counts[k]
        if k == key and n <= fail_times:
            raise exc or OSError(f"connection reset while fetching {k}")
        return real_get(k)

    monkeypatch.setattr(ingest.r2, "get_object", flaky)
    monkeypatch.setattr(ingest.time, "sleep", slept.append)
    return counts, slept


@pytest.mark.parametrize("workers", [1, 4])
def test_one_failed_object_does_not_abort_the_run(
    fresh_db, mini_r2_env, monkeypatch, workers
):
    """A single unfetchable object costs that object, not the ingest.

    Both the sequential and the pooled path are exercised: they collect
    results differently (a generator consumed in the persist loop vs
    as_completed), and each used to let the exception escape.
    """
    monkeypatch.setenv("INGEST_WORKERS", str(workers))
    counts, _ = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 1
    assert result["inserted"] == 4, "the other four files must still persist"
    assert result["error"] == f"1 object failed after retries: {_FLAKY_KEY}"
    assert counts[_FLAKY_KEY] == ingest.FETCH_ATTEMPTS
    with db.viz_conn() as c:
        keys = [r[0] for r in c.execute(
            "SELECT file_key FROM files ORDER BY file_key"
        ).fetchall()]
    assert _FLAKY_KEY not in keys
    assert len(keys) == 4


def test_per_object_failure_still_rebuilds_derived_state(
    fresh_db, mini_r2_env, monkeypatch
):
    """The regression that matters: derived state must not be left stale.

    recompute_canonical() and the rollups describe whatever `records` now
    holds. Gating them on a flawless run meant one dropped connection left
    `usage_rollup` / `tool_rollup` describing the PREVIOUS dataset and
    is_canonical un-recomputed until some later run happened to be clean.
    """
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rollup_before = _scalar(c,
            "SELECT COUNT(*) FROM usage_rollup"
        )
        dupes_before = _scalar(c,
            "SELECT COUNT(*) FROM records WHERE NOT is_canonical"
        )
    assert rollup_before > 0 and dupes_before > 0, "fixture proves nothing"

    with db.viz_conn() as c:
        # Tool calls hang off the file whose fetch fails, so the reparse
        # never deletes them and tool_rollup has something to rebuild from.
        c.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, "
            "is_error) VALUES (%s, 9001, 0, now(), 'Read', false)",
            (_FLAKY_KEY,),
        )
        # Wreck every piece of derived state, then prove the run restores it.
        c.execute("TRUNCATE usage_rollup")
        c.execute("TRUNCATE tool_rollup")
        c.execute("UPDATE records SET is_canonical = TRUE")
        c.commit()

    monkeypatch.setenv("PARSER_VERSION", "2")
    _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)
    result = ingest.run_ingest(trigger="manual")
    assert result["failed"] == 1
    assert result["reparsed"] == 4

    with db.viz_conn() as c:
        rollup_after = _scalar(c,
            "SELECT COUNT(*) FROM usage_rollup"
        )
        dupes_after = _scalar(c,
            "SELECT COUNT(*) FROM records WHERE NOT is_canonical"
        )
        tool_rollup_after = _scalar(c,
            "SELECT COUNT(*) FROM tool_rollup"
        )
    assert rollup_after == rollup_before, "usage_rollup was not rebuilt"
    assert dupes_after == dupes_before, "is_canonical was not recomputed"
    assert tool_rollup_after > 0, "tool_rollup was not rebuilt"


def test_a_transient_fetch_failure_is_retried_and_recovers(
    fresh_db, mini_r2_env, monkeypatch
):
    """Two failed GETs then a good one: the file lands, the run is clean."""
    counts, slept = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=2)

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 0
    assert result["error"] is None
    assert result["inserted"] == 5
    assert counts[_FLAKY_KEY] == 3
    assert slept == [0.5, 1.0], "exponential backoff between attempts"
    with db.viz_conn() as c:
        n = _scalar(c,
            "SELECT COUNT(*) FROM files WHERE file_key = %s", (_FLAKY_KEY,)
        )
    assert n == 1


def test_fetch_gives_up_after_three_attempts(
    fresh_db, mini_r2_env, monkeypatch
):
    """The retry is bounded — it must not spin on a genuinely dead object."""
    counts, slept = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 3
    assert slept == [0.5, 1.0]
    assert result["failed"] == 1
    assert "connection reset" not in (result["error"] or ""), \
        "the summary names keys, not stack noise"
    assert _FLAKY_KEY in result["error"]


_CORRUPT_XZ_KEY = "projC/sess-E/sess-E.jsonl.xz"


def test_a_corrupt_xz_object_is_one_failure_not_a_dead_run(
    fresh_db, mini_r2_env, monkeypatch
):
    """Real invalid bytes under a `.xz` key, not a monkeypatched raise.

    r2.get_object inflates `.xz` transparently, so lzma raises from inside
    the fetch — and lzma.LZMAError is not an OSError. Every production
    object is `.xz`, so classifying it as "not transient, therefore a bug"
    would abort the entire run over one truncated upload: this issue's
    original failure mode, for 100% of the bucket.
    """
    corrupt = mini_r2_env / "projC" / "sess-E" / "sess-E.jsonl.xz"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"\xfd7zXZ\x00 this is not a valid xz stream \x00\x01")
    with pytest.raises(lzma.LZMAError):
        lzma.decompress(corrupt.read_bytes())   # the fixture must really be corrupt

    counts, slept = _patch_fetch(monkeypatch, _CORRUPT_XZ_KEY, fail_times=0)

    result = ingest.run_ingest(trigger="manual")

    assert result["r2_listed"] == 6
    assert result["failed"] == 1
    assert result["error"] == (
        f"1 object failed after retries: {_CORRUPT_XZ_KEY}"
    )
    assert result["inserted"] == 5, "the intact objects are still persisted"
    assert counts[_CORRUPT_XZ_KEY] == 1, "a corrupt object must not be re-fetched"
    assert not slept, "and must not sleep between attempts it does not make"
    with db.viz_conn() as c:
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
        stored = _scalar(c,
            "SELECT COUNT(*) FROM files WHERE file_key = %s", (_CORRUPT_XZ_KEY,)
        )
    assert rollup > 0, "derived state must still be rebuilt"
    assert stored == 0


def test_a_programming_error_in_the_fetch_is_not_retried(
    fresh_db, mini_r2_env, monkeypatch
):
    """A bug is not a transient, and must not be dressed up as one.

    Retrying a TypeError sleeps 1.5s per object and books it as a
    per-object failure — at 9,213 objects that is a silent multi-hour
    "partial run" instead of one loud traceback.
    """
    counts, slept = _patch_fetch(
        monkeypatch, _FLAKY_KEY, fail_times=99,
        exc=TypeError("get_object() takes 1 positional argument but 2 were given"),
    )

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 1, "a bug must not be retried"
    assert not slept, "and must not sleep"
    assert result["failed"] == 0, "it is not a per-object failure"
    assert result["error"].startswith("FatalFetchError:"), result["error"]
    assert "TypeError" in result["error"], "the type must survive into the run"
    with db.viz_conn() as c:
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
    assert rollup == 0, "a fatal run must not rebuild derived state"


def test_a_parse_failure_is_not_retried(fresh_db, mini_r2_env, monkeypatch):
    """Parsing is deterministic: re-fetching the same bytes buys nothing."""
    real_get = ingest.r2.get_object
    counts: Counter = Counter()
    lock = threading.Lock()

    def counting(k):
        with lock:
            counts[k] += 1
        return real_get(k)

    real_parse = ingest.parse.parse_file

    def boom(file_key, blob):
        if file_key == _FLAKY_KEY:
            raise ValueError("malformed line 3")
        return real_parse(file_key, blob)

    monkeypatch.setattr(ingest.r2, "get_object", counting)
    monkeypatch.setattr(ingest.parse, "parse_file", boom)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: pytest.fail(
        "a parse failure must not sleep on a retry"
    ))

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 1, "the GET must not be repeated"
    assert result["failed"] == 1
    assert result["inserted"] == 4
    assert "ValueError" not in (result["error"] or "")


def test_failure_summary_truncates_a_long_key_list():
    """A 9,213-file run losing its connection must not write a novel into
    ingest_runs.error."""
    failed = [(f"p/s{i}/s{i}.jsonl", "OSError: boom") for i in range(9)]
    summary = ingest.failure_summary(failed)
    assert summary is not None
    assert summary.startswith("9 objects failed after retries: ")
    assert summary.endswith(", ... (+4 more)")
    assert summary.count(".jsonl") == ingest.FAILURE_KEYS_IN_SUMMARY
    assert ingest.failure_summary([]) is None


def test_a_second_ingest_is_skipped_while_one_is_running(fresh_db, mini_r2_env):
    """The hourly cron fires regardless of whether a run is still going. A
    PARSER_VERSION bump makes a run take ~40min, so a cron landed on top of a
    startup reparse and both walked the whole bucket — duplicate GETs,
    duplicate parses, two sets of rollup rebuilds, and a progress readout that
    went backwards. A concurrent run must decline, not pile on."""
    started = threading.Event()
    release = threading.Event()
    seen = {}

    real = ingest.run_ingest_locked

    def slow(trigger):
        started.set()
        release.wait(timeout=30)
        return real(trigger)

    ingest.run_ingest_locked = slow
    try:
        t = threading.Thread(target=lambda: seen.update(first=ingest.run_ingest("startup")))
        t.start()
        assert started.wait(timeout=10), "first run never entered"
        # Second run arrives while the first still holds the lock.
        second = ingest.run_ingest(trigger="cron")
        release.set()
        t.join(timeout=60)
    finally:
        ingest.run_ingest_locked = real

    assert second.get("skipped") is True, second
    assert "already running" in second.get("reason", "")
    assert seen["first"].get("skipped") is not True, "the first run must NOT be skipped"


def test_ingest_runs_again_once_the_lock_is_free(fresh_db, mini_r2_env):
    """The skip is per-overlap, not sticky — the lock must be released."""
    ingest.run_ingest(trigger="manual")
    again = ingest.run_ingest(trigger="manual")
    assert again.get("skipped") is not True, again
