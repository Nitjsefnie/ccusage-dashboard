import importlib.util
import json
import os
import shutil
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from backend import api, cache, db, ingest, pricing

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plot_db_module():
    """Import scripts/plots/ccusage_plot_db.py by path (not a package)."""
    path = _REPO_ROOT / "scripts/plots/ccusage_plot_db.py"
    spec = importlib.util.spec_from_file_location("ccusage_plot_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_export_argv_period_and_project():
    argv = api.build_export_argv("7d", "myproj", "/tmp/out.png")
    assert "/tmp/out.png" in argv
    assert argv[argv.index("-p") + 1] == "7d"
    assert argv[argv.index("--project") + 1] == "myproj"
    assert "--db-url" not in argv  # DSN comes from inherited env, not argv
    assert "--all" not in argv


def test_build_export_argv_all_and_no_project():
    argv = api.build_export_argv("all", None, "/tmp/out.png")
    assert "--all" in argv
    assert "-p" not in argv
    assert "--project" not in argv
    assert "--db-url" not in argv


def test_export_returns_png_attachment(app_with_data, monkeypatch):
    captured = {}

    async def fake_render(argv, out_path):
        captured["argv"] = argv
        # Simulate the script writing a PNG.
        with open(out_path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"fake")

    monkeypatch.setattr(api, "_render_export", fake_render)

    resp = app_with_data.get("/api/export?range=7d")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"\x89PNG")
    assert "-p" in captured["argv"] and "7d" in captured["argv"]


def test_export_bad_range_400(app_with_data, monkeypatch):
    async def fake_render(argv, out_path):  # should never be called
        raise AssertionError("render must not run on bad range")
    monkeypatch.setattr(api, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=banana")
    assert resp.status_code == 400


def test_export_render_timeout_returns_503(app_with_data, monkeypatch):
    async def fake_render(argv, out_path):
        raise HTTPException(503, "export render timed out")
    monkeypatch.setattr(api, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=7d")
    assert resp.status_code == 503


def test_export_render_failure_returns_500(app_with_data, monkeypatch):
    async def fake_render(argv, out_path):
        raise HTTPException(500, "export render failed")
    monkeypatch.setattr(api, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=7d")
    assert resp.status_code == 500


def test_plot_db_project_filter_subsets_events(app_with_data):
    """load_events(project=...) returns a strict subset of all-projects,
    and every returned event belongs to the requested project."""
    mod = _load_plot_db_module()
    mod.DB_URL = os.environ["DATABASE_URL_VIZ"]

    all_events = mod.load_events(None, None)
    assert all_events, "fixture should yield records"

    # Discover a real project_id from the test DB.
    with closing(psycopg.connect(mod.DB_URL)) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT project_id FROM files ORDER BY 1")
        project_ids = [r[0] for r in cur.fetchall()]
    assert len(project_ids) >= 2, "mini fixture has 2 projects"
    target = project_ids[0]

    filtered = mod.load_events(None, None, project=target)
    assert filtered, "project filter should still yield records"
    assert len(filtered) < len(all_events), "filter must drop the other project"


def _build_api_client(mp, test_db: str):
    """Fresh DB + mini R2 + ingest, yielding a TestClient on the api router.

    Auth is bypassed by mounting only the router into a clean app.
    """
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    mp.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-api-")
    shutil.copytree(src, Path(tmp) / "r2")
    mp.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    db.reset_viz_pool()

    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api.router)

    yield TestClient(a)

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


# Module-scoped: this setup (dropdb, createdb, schema, copy the R2 tree,
# a full ingest incl. recompute_canonical + rebuild_rollup) ran per test
# and was ~2-5s of pure `setup` on every one of ~40 read-only tests —
# the whole reason the suite took 170s. Tests that WRITE must not share
# it; they take `app_with_fresh_data` below.
@pytest.fixture(scope="module", name="app_with_data")
def _app_with_data_fixture():
    mp = pytest.MonkeyPatch()          # monkeypatch itself is function-scoped
    try:
        yield from _build_api_client(mp, "claudit_test_api")
    finally:
        mp.undo()


@pytest.fixture(name="app_with_fresh_data")
def _app_with_fresh_data_fixture():
    """Function-scoped variant for tests that mutate rows, so they cannot
    contaminate the shared module-scoped client."""
    mp = pytest.MonkeyPatch()
    try:
        yield from _build_api_client(mp, "claudit_test_api_mut")
    finally:
        mp.undo()


def test_projects(app_with_data):
    r = app_with_data.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    pids = sorted(p["project_id"] for p in body["projects"])
    assert pids == ["projA", "projB"]
    # session_count + total_cost drive the picker chip and its ordering.
    # file_count was dropped: nothing rendered it (and it was reporting
    # the joined record count, not the file count).
    for p in body["projects"]:
        assert "session_count" in p and "total_cost" in p
        assert "file_count" not in p


def test_projects_range_scoped_ordering_and_zero_cost_exclusion(app_with_fresh_data):
    """SV-ISSUE-6: /api/projects orders by RANGE-scoped cost and drops any
    project whose ALL-TIME cost is 0 — two different aggregates that must
    not be conflated. A project with real all-time cost but nothing in the
    selected range stays listed, sorted last, with its reported (range)
    cost at 0."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, last_seen_at) VALUES "
            "('projNeverCost', 'projNeverCost', now(), now()), "
            "('projOldCost', 'projOldCost', now(), now()), "
            "('projRecentCost', 'projRecentCost', now(), now())"
        )
        cur.execute(
            "INSERT INTO usage_rollup (session_id, project_id, hour, model, is_main, "
            "first_ts, last_ts, requests, cost_usd) VALUES "
            # Never cost anything, ever — must be excluded outright, even
            # though it has a usage_rollup row (a row with cost 0 is not
            # the same as "no usage").
            "('sess-zero', 'projNeverCost', now() - interval '1 day', 'm', TRUE, now(), now(), 1, 0), "
            # Real historical cost (bigger than projRecentCost's), but 60
            # days old — outside a 7d range. Must still be LISTED (all-time
            # cost is nonzero) but sorted to the bottom with 0 range cost.
            "('sess-old', 'projOldCost', now() - interval '60 days', 'm', TRUE, now(), now(), 1, 5.00), "
            # Smaller all-time cost, but entirely inside the 7d range —
            # must outrank projOldCost despite the smaller all-time total,
            # proving the ordering is RANGE-scoped, not all-time.
            "('sess-recent', 'projRecentCost', now() - interval '1 hour', 'm', TRUE, now(), now(), 1, 1.00)"
        )
        conn.commit()

    r = app_with_fresh_data.get("/api/projects?range=7d")
    assert r.status_code == 200
    body = r.json()
    by_id = {p["project_id"]: p for p in body["projects"]}

    assert "projNeverCost" not in by_id, \
        "all-time-zero-cost project must be excluded, not just range-filtered"
    assert "projOldCost" in by_id, \
        "a historically-costly project must stay listed even at 0 range cost"
    assert by_id["projOldCost"]["total_cost"] == 0.0
    assert by_id["projRecentCost"]["total_cost"] == 1.0

    pids_in_order = [p["project_id"] for p in body["projects"]]
    assert pids_in_order.index("projRecentCost") < pids_in_order.index("projOldCost"), (
        "ordering must follow range-scoped cost, not all-time cost — "
        "projOldCost's larger all-time total must NOT outrank projRecentCost"
    )

    # Widening the range to include projOldCost's usage re-sorts it above
    # projRecentCost — proving the list is genuinely range-scoped, not a
    # fixed order computed once.
    r_wide = app_with_fresh_data.get("/api/projects?range=90d")
    assert r_wide.status_code == 200
    body_wide = r_wide.json()
    by_id_wide = {p["project_id"]: p for p in body_wide["projects"]}
    assert by_id_wide["projOldCost"]["total_cost"] == 5.0
    pids_wide = [p["project_id"] for p in body_wide["projects"]]
    assert pids_wide.index("projOldCost") < pids_wide.index("projRecentCost")


def test_cache_per_model_shape(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "per_model" in body and "session_total" in body
    assert "top_output" in body and "top_cache_create" in body and "top_cache_read" in body
    if body["per_model"]:
        m = body["per_model"][0]
        assert {"model", "turns", "fresh", "cache_create", "cache_read",
                "output", "eph5", "eph1h", "hit_rate_pct",
                "cost_total", "cost_buckets"} <= set(m)
        assert {"fresh", "create_5m", "create_1h", "read", "output"} == set(m["cost_buckets"])


def test_cache_dedups_cross_file_uuid(app_with_data):
    """sess-C main + agent peer both have shared-uuid-1; sess-D main also
    has it. Records table holds 3 rows for that uuid; DISTINCT ON dedups
    to 1 in the per_model totals.

    sess-C main has input=1000, output=500 (single record).
    sess-C agent has input=1000, output=500 (same uuid → dedup'd).
    sess-D main has 2 records: shared-uuid-1 (1000/500, dedup'd) +
                                sess-D-only (50/25, kept).

    After cross-file dedup:
      shared-uuid-1 winner = lexicographically-first file_key, which is
      claude/projB/sess-C/agent-aaaa.jsonl (agent- < sess-)
      WAIT — actually 'a' < 's' so the agent file IS lexicographically
      first. Either way, ONE row claims the shared uuid; the other two
      drop. The remaining tally for projB: 1000 + 50 input, 500 + 25 output.
    """
    r = app_with_data.get("/api/cache?range=3650d&project=projB")
    body = r.json()
    assert body["session_total"]["fresh"] == 1050   # 1000 + 50
    assert body["session_total"]["output"] == 525   # 500 + 25
    assert body["session_total"]["turns"] == 2       # one shared + one unique


def test_cache_top_n_limited_to_10(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    body = r.json()
    assert len(body["top_output"]) <= 10
    assert len(body["top_cache_create"]) <= 10
    assert len(body["top_cache_read"]) <= 10


def test_cache_bad_range_400(app_with_data):
    r = app_with_data.get("/api/cache?range=abc")
    assert r.status_code == 400


def test_cache_session_total_matches_per_model_sum(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    body = r.json()
    sum_turns = sum(m["turns"] for m in body["per_model"])
    sum_cost = round(sum(m["cost_total"] for m in body["per_model"]), 4)
    assert body["session_total"]["turns"] == sum_turns
    assert body["session_total"]["cost_total"] == sum_cost


def test_cache_session_total_estimated_rate_true_when_any_model_estimated(
    app_with_data, monkeypatch
):
    """Fixture data carries two real models (claude-opus-4-7 and
    claude-sonnet-4-5), both exact matches normally. Drop one from the rate
    table so it resolves as "default" (estimated) while the other stays
    exact — a genuine mixed session, same shape a live account would show
    the day a new model ships before the rate table is updated.
    """
    patched = {k: v for k, v in pricing.MODEL_RATES.items() if k != "claude-sonnet-4-5"}
    monkeypatch.setattr(pricing, "MODEL_RATES", patched)

    body = app_with_data.get("/api/cache?range=3650d").json()
    assert len(body["per_model"]) >= 2, body["per_model"]
    flags = {m["model"]: m["estimated_rate"] for m in body["per_model"]}
    assert flags["claude-sonnet-4-5"] is True
    assert flags["claude-opus-4-7"] is False
    assert body["session_total"]["estimated_rate"] is True


def test_cache_session_total_estimated_rate_false_when_all_exact(app_with_data):
    body = app_with_data.get("/api/cache?range=3650d").json()
    assert body["per_model"], "fixture produced no per-model rows"
    assert all(m["estimated_rate"] is False for m in body["per_model"])
    assert body["session_total"]["estimated_rate"] is False


def test_cache_session_total_estimated_rate_false_for_empty_per_model(app_with_data):
    """An empty per_model list must not crash any(...) over it, and must not
    default-True a total with no contributing models."""
    r = app_with_data.get("/api/cache?range=3650d&model=nonexistent-model-zzz")
    assert r.status_code == 200
    body = r.json()
    assert body["per_model"] == []
    assert body["session_total"]["estimated_rate"] is False


def test_transcript_streams(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-ndjson"
    first = r.text.split("\n")[0]
    assert "type" in json.loads(first)


def test_transcript_etag_header(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert "etag" in {k.lower() for k in r.headers.keys()}


def test_transcript_404(app_with_data):
    r = app_with_data.get("/api/sessions/does-not-exist/transcript")
    assert r.status_code == 404


def test_sidecar_path_validation(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/tool-results/x.txt"},
    )
    assert r.status_code == 200
    assert r.text.strip() == "tool output"
    r2 = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "../../../etc/passwd"},
    )
    assert r2.status_code == 400


def test_sidecar_absolute_path_rejected(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "/etc/passwd"},
    )
    assert r.status_code == 400


def test_sidecar_missing_file_404(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/does-not-exist.txt"},
    )
    assert r.status_code == 404


def test_context_growth_agg_shape(app_with_data):
    r = app_with_data.get("/api/context-growth/agg?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "per_turn" in body and "per_session_final" in body
    for k in ("n", "mean", "p50", "p90", "p99", "max"):
        assert k in body["per_turn"]
        assert k in body["per_session_final"]


def test_context_growth_session_returns_canonical_array(app_with_data):
    """Mini fixture sess-A has 1 turn (single_turn.jsonl). Verify the
    per-turn array is returned with the canonical shape."""
    r = app_with_data.get("/api/context-growth/session/sess-A")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sess-A"
    assert "turns" in body and isinstance(body["turns"], list)
    if body["turns"]:
        t = body["turns"][0]
        assert {"idx", "ts", "line", "input", "output", "delta"} == set(t)
    assert body["total_turns"] == len(body["turns"])


def test_context_growth_session_404(app_with_data):
    r = app_with_data.get("/api/context-growth/session/does-not-exist")
    assert r.status_code == 404


def test_tool_error_rate_returns_expected_shape(app_with_data):
    r = app_with_data.get("/api/tool-error-rate?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "range" in body
    assert "bucket_s" in body
    assert "buckets" in body
    assert isinstance(body["buckets"], list)
    for b in body["buckets"]:
        assert {"ts", "model", "tool", "n_total", "n_error"} <= set(b.keys())
        assert b["n_error"] <= b["n_total"]


@pytest.fixture(name="app_with_rl_data")
def _app_with_rl_data_fixture(monkeypatch):
    """Fresh DB + R2 mirror plus one session whose file mtime is current
    but which carries both an in-range and an out-of-range rate-limit hit.

    The out-of-range hit reproduces the bug where /api/dashboard filtered
    rate-limit hits by file mtime (r2_last_modified) rather than the hit's
    own ts. Yields (client, in_range_ts, out_of_range_ts).
    """
    test_db = "claudit_test_api_rl"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    tmp = tempfile.mkdtemp(prefix="sv-api-rl-")
    shutil.copytree(_REPO_ROOT / "fixtures/r2_mini", Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    now = datetime.now(timezone.utc)
    in_range = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_range = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _rl(ts, uid):
        return json.dumps({
            "type": "assistant", "timestamp": ts, "uuid": uid,
            "isApiErrorMessage": True, "error": "rate_limit",
            "message": {"role": "assistant", "content": [{
                "type": "text",
                "text": "Claude usage limit reached - you are out of "
                        "extra usage.",
            }]},
        })

    sess_dir = Path(tmp) / "r2" / "claude" / "projA" / "sess-RL"
    sess_dir.mkdir(parents=True)
    (sess_dir / "sess-RL.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": in_range, "uuid": "rl-u1",
                    "message": {"role": "user", "content": "hi"}}) + "\n"
        + json.dumps({
            "type": "assistant", "timestamp": in_range, "uuid": "rl-a1",
            "requestId": "rl-req-1",
            "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 10, "output_tokens": 20,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}}) + "\n"
        + _rl(in_range, "rl-h1") + "\n"
        + _rl(out_range, "rl-h2") + "\n"
    )

    db.reset_viz_pool()

    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api.router)

    yield TestClient(a), in_range, out_range

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


def test_dashboard_returns_prompts_and_turns_totals(app_with_data):
    """total_prompts sums files.prompt_count; total_turns sums files.turn_count.
    Mini r2 has one real user prompt (sess-A) and five usage-bearing files
    (sess-A, sess-B, sess-C main, sess-C agent, sess-D), each producing
    a single ctx_turn entry."""
    body = app_with_data.get("/api/dashboard?range=3650d").json()
    assert body["total_prompts"] == 1
    assert body["total_turns"] == 5

    # Project filter scopes both counts.
    body_b = app_with_data.get("/api/dashboard?range=3650d&project=projB").json()
    assert body_b["total_prompts"] == 0
    assert body_b["total_turns"] == 3


def test_dashboard_excludes_rate_limit_hits_older_than_range(app_with_rl_data):
    client, in_range, out_range = app_with_rl_data

    hits_30d = [h["ts"] for h in
                client.get("/api/dashboard?range=30d").json()["rate_limit_hits"]]
    assert in_range in hits_30d
    # The 45-day-old hit must not appear: it sits outside the 30d window
    # even though its file's r2_last_modified (mtime) is current.
    assert out_range not in hits_30d

    hits_all = [h["ts"] for h in
                client.get("/api/dashboard?range=3650d").json()["rate_limit_hits"]]
    assert in_range in hits_all
    assert out_range in hits_all


def test_a_malformed_hit_ts_does_not_take_down_the_dashboard(app_with_fresh_data):
    """Junk in a hit's `ts` must be excluded, not raised.

    Casting it is what filters on the hit's own time, but an unguarded
    `::timestamptz` RAISES on a malformed value, and the traceback escapes
    the handler — so one bad `ts` anywhere in files.rate_limit_hits would
    500 the entire dashboard, every panel, not merely this one.

    The second block below is the one that matters: those values are
    timestamp-SHAPED and still raise on cast, so a guard that pattern-
    matches the shape passes them straight through to the cast it was
    meant to protect. Only real input validation excludes them.
    """
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE files SET r2_last_modified = now(), "
            "       rate_limit_hits = %s::jsonb "
            " WHERE file_key = (SELECT MIN(file_key) FROM files WHERE is_main)",
            (json.dumps([
                # Not timestamp-shaped at all.
                {"ts": "not-a-timestamp", "content": "junk ts"},
                {"ts": 123, "content": "numeric ts"},
                {"ts": "", "content": "empty ts"},
                {"ts": None, "content": "null ts"},
                {"content": "no ts key at all"},
                # Timestamp-shaped, but not valid timestamps.
                {"ts": "2026-13-45T99:99:99Z", "content": "month 13, day 45"},
                {"ts": "2026-02-30T00:00:00Z", "content": "30th of february"},
                {"ts": "2026-01-01 25:00:00Z", "content": "hour 25"},
                {"ts": "2026-01-01T00:00:00Z lolwat", "content": "trailing junk"},
                # Valid, and on either side of the range boundary.
                {"ts": (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"), "content": "good hit"},
                {"ts": "1998-01-01T00:00:00Z", "content": "too old"},
            ]),),
        )
        conn.commit()

    cache.response_cache.clear()
    r = app_with_fresh_data.get("/api/dashboard?range=30d")
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    assert [h["content"] for h in r.json()["rate_limit_hits"]] == ["good hit"]


def test_dashboard_response_is_cached_and_fresh_bypasses(app_with_fresh_data):
    cache.response_cache.clear()
    first = app_with_fresh_data.get("/api/dashboard?range=all").json()

    # Mutate the DB underneath the cache: delete every record. usage_rollup
    # is derived state that ingest rebuilds from `records`, so emptying the
    # data means emptying both — leaving the rollup behind would just be
    # reading a stale pre-aggregate, which is not what this test is about.
    with db.viz_conn() as c:
        c.execute("DELETE FROM records")
        c.execute("DELETE FROM usage_rollup")

    cached = app_with_fresh_data.get("/api/dashboard?range=all").json()
    assert cached == first                       # stale-but-cached payload

    fresh = app_with_fresh_data.get("/api/dashboard?range=all&fresh=1").json()
    assert fresh["cost_by_model"] == []          # fresh=1 sees the empty DB


def test_dashboard_cost_by_project_shape(app_with_data):
    """cost_by_project mirrors cost_by_model: range-filtered, sorted by
    cost desc, zero-cost rows excluded, at most 10 project rows plus a
    single "Other (N projects)" fold."""
    body = app_with_data.get("/api/dashboard?range=3650d").json()
    cbp = body["cost_by_project"]
    assert {"projA", "projB"} <= {r["project"] for r in cbp}
    costs = [r["cost_usd"] for r in cbp]
    assert all(c > 0 for c in costs)
    assert costs == sorted(costs, reverse=True)
    named = [r for r in cbp if not r["project"].startswith("Other (")]
    assert len(named) <= 10
    others = [r for r in cbp if r["project"].startswith("Other (")]
    assert len(others) <= 1

    # A project filter scopes the breakdown to that one project.
    body_b = app_with_data.get("/api/dashboard?range=3650d&project=projB").json()
    assert {r["project"] for r in body_b["cost_by_project"]} == {"projB"}


def test_dashboard_cost_by_project_omitted_for_guest(app_with_data):
    """The guest gate is SERVER-side. /api/dashboard is guest-accessible,
    so per-project names/costs — the very data the 403s on /api/projects
    and on project= exist to withhold (session.auth_middleware) — must be
    missing from the response body itself, not merely unrendered by the
    frontend.

    This suite mounts only api.router (auth bypassed), so a guest is
    simulated by a middleware setting the SAME request.state.is_guest
    flag the real middleware sets. The guest call reuses the non-guest
    call's query params, so it is served from the SHARED response cache —
    proving the strip happens per-request, outside the cached payload."""
    body = app_with_data.get("/api/dashboard?range=3650d").json()
    assert "cost_by_project" in body

    a = FastAPI()

    @a.middleware("http")
    async def set_guest_flag(request: Request, call_next):
        request.state.is_guest = True
        return await call_next(request)

    a.include_router(api.router)
    guest = TestClient(a).get("/api/dashboard?range=3650d")
    assert guest.status_code == 200
    assert "cost_by_project" not in guest.json()
    # The rest of the payload is untouched.
    assert "cost_by_model" in guest.json()


# ---------------------------------------------------------------- heatmap

def _insert_tz_probe_rows():
    """Two records with a unique model, one in winter (CET, UTC+1) and one
    in summer (CEST, UTC+2), to prove the endpoint is DST-aware."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, last_seen_at) "
            "VALUES ('projTZ', 'projTZ', now(), now()) ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, r2_etag, "
            "r2_size_bytes, r2_last_modified, parsed_at, parser_version) "
            "VALUES ('projTZ/tz.jsonl', 'projTZ', 'tzsess', TRUE, 'etag-tz', 1, now(), now(), 'test')"
        )
        cur.execute(
            "INSERT INTO records (file_key, line_num, uuid, ts, model, output_tokens, cost_usd) VALUES "
            # 2026-01-15 is a Thursday (ISODOW 4); 10:30Z in CET (UTC+1) is 11:30 local.
            "('projTZ/tz.jsonl', 1, 'uuid-tz-winter', '2026-01-15T10:30:00Z', 'tz-probe-model', 10, 0.01), "
            # 2026-07-15 is a Wednesday (ISODOW 3); 10:30Z in CEST (UTC+2) is 12:30 local.
            "('projTZ/tz.jsonl', 2, 'uuid-tz-summer', '2026-07-15T10:30:00Z', 'tz-probe-model', 20, 0.02)"
        )
        conn.commit()

    # /api/activity-heatmap reads usage_rollup, which ingest rebuilds from
    # `records`. These rows were inserted behind ingest's back, so rebuild
    # it here or the endpoint cannot see them (SV-ROLLUP: the rollup is
    # derived state; anything mutating `records` outside ingest must
    # rebuild it).
    ingest.rebuild_rollup()


def test_activity_heatmap_shape(app_with_data):
    r = app_with_data.get("/api/activity-heatmap?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert body["tz"] == "Europe/Prague"
    assert body["cells"], "mini fixture must produce at least one cell"
    for c in body["cells"]:
        assert 1 <= c["dow"] <= 7
        assert 0 <= c["hour"] <= 23
        assert c["requests"] >= 1
        assert c["output_tokens"] >= 0
        assert c["cost_usd"] >= 0


def test_activity_heatmap_requests_match_dashboard(app_with_data):
    # Both endpoints read through the same DISTINCT ON (uuid) dedup, so
    # total request counts must agree for the same range.
    heat = app_with_data.get("/api/activity-heatmap?range=3650d").json()
    dash = app_with_data.get("/api/dashboard?range=3650d").json()
    assert sum(c["requests"] for c in heat["cells"]) == \
           sum(h["requests"] for h in dash["hourly"])


def test_activity_heatmap_dst_awareness(app_with_fresh_data):
    _insert_tz_probe_rows()
    r = app_with_fresh_data.get("/api/activity-heatmap?range=3650d&model=tz-probe-model")
    assert r.status_code == 200
    cells = {(c["dow"], c["hour"]): c for c in r.json()["cells"]}
    assert set(cells) == {(4, 11), (3, 12)}, cells
    assert cells[(4, 11)]["requests"] == 1   # winter: 10:30Z -> 11:30 CET, Thu
    assert cells[(3, 12)]["requests"] == 1   # summer: 10:30Z -> 12:30 CEST, Wed
    assert cells[(3, 12)]["output_tokens"] == 20


def test_activity_heatmap_project_filter(app_with_data):
    both = app_with_data.get("/api/activity-heatmap?range=3650d").json()
    one = app_with_data.get("/api/activity-heatmap?range=3650d&project=projA").json()
    assert sum(c["requests"] for c in one["cells"]) < \
           sum(c["requests"] for c in both["cells"])


def test_activity_heatmap_bad_range_400(app_with_data):
    assert app_with_data.get("/api/activity-heatmap?range=bogus").status_code == 400
