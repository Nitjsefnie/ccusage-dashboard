import importlib.util
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plot_db_module():
    """Import scripts/plots/ccusage_plot_db.py by path (not a package)."""
    path = _REPO_ROOT / "scripts/plots/ccusage_plot_db.py"
    spec = importlib.util.spec_from_file_location("ccusage_plot_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_export_argv_period_and_project():
    from backend import api
    argv = api._build_export_argv("7d", "myproj", "/tmp/out.png")
    assert "/tmp/out.png" in argv
    assert argv[argv.index("-p") + 1] == "7d"
    assert argv[argv.index("--project") + 1] == "myproj"
    assert "--db-url" not in argv  # DSN comes from inherited env, not argv
    assert "--all" not in argv


def test_build_export_argv_all_and_no_project():
    from backend import api
    argv = api._build_export_argv("all", None, "/tmp/out.png")
    assert "--all" in argv
    assert "-p" not in argv
    assert "--project" not in argv
    assert "--db-url" not in argv


def test_export_returns_png_attachment(app_with_data, monkeypatch):
    from backend import api

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
    from backend import api
    async def fake_render(argv, out_path):  # should never be called
        raise AssertionError("render must not run on bad range")
    monkeypatch.setattr(api, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=banana")
    assert resp.status_code == 400


def test_export_render_timeout_returns_503(app_with_data, monkeypatch):
    from backend import api
    from fastapi import HTTPException
    async def fake_render(argv, out_path):
        raise HTTPException(503, "export render timed out")
    monkeypatch.setattr(api, "_render_export", fake_render)
    resp = app_with_data.get("/api/export?range=7d")
    assert resp.status_code == 503


def test_export_render_failure_returns_500(app_with_data, monkeypatch):
    from backend import api
    from fastapi import HTTPException
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
    import psycopg
    with psycopg.connect(mod.DB_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT project_id FROM files ORDER BY 1")
        project_ids = [r[0] for r in cur.fetchall()]
    assert len(project_ids) >= 2, "mini fixture has 2 projects"
    target = project_ids[0]

    filtered = mod.load_events(None, None, project=target)
    assert filtered, "project filter should still yield records"
    assert len(filtered) < len(all_events), "filter must drop the other project"


@pytest.fixture
def app_with_data(monkeypatch):
    """Spin up a fresh DB + mini R2, ingest, return an authed TestClient.

    Bypasses auth via a clean FastAPI app with only the api router.
    """
    test_db = "claudit_test_api"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-api-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    from backend import db as _db
    if _db._VIZ is not None:
        try:
            _db._VIZ.close()
        except Exception:
            pass
    _db._VIZ = None

    from backend import ingest
    ingest.run_ingest(trigger="manual")

    from fastapi import FastAPI
    from backend import api as api_mod
    a = FastAPI()
    a.include_router(api_mod.router)

    yield TestClient(a)

    if _db._VIZ is not None:
        try:
            _db._VIZ.close()
        except Exception:
            pass
    _db._VIZ = None
    shutil.rmtree(tmp)
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


def test_projects(app_with_data):
    r = app_with_data.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    pids = sorted(p["project_id"] for p in body["projects"])
    assert pids == ["projA", "projB"]
    # Each project carries file_count + total_cost
    for p in body["projects"]:
        assert "file_count" in p and "total_cost" in p


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


def test_transcript_streams(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-ndjson"
    import json
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


@pytest.fixture
def app_with_rl_data(monkeypatch):
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

    from backend import db as _db
    if _db._VIZ is not None:
        try:
            _db._VIZ.close()
        except Exception:
            pass
    _db._VIZ = None

    from backend import ingest
    ingest.run_ingest(trigger="manual")

    from fastapi import FastAPI
    from backend import api as api_mod
    a = FastAPI()
    a.include_router(api_mod.router)

    yield TestClient(a), in_range, out_range

    if _db._VIZ is not None:
        try:
            _db._VIZ.close()
        except Exception:
            pass
    _db._VIZ = None
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


def test_dashboard_response_is_cached_and_fresh_bypasses(app_with_data):
    from backend import cache, db

    cache.response_cache.clear()
    first = app_with_data.get("/api/dashboard?range=all").json()

    # Mutate the DB underneath the cache: delete every record.
    with db.viz_conn() as c:
        c.execute("DELETE FROM records")

    cached = app_with_data.get("/api/dashboard?range=all").json()
    assert cached == first                       # stale-but-cached payload

    fresh = app_with_data.get("/api/dashboard?range=all&fresh=1").json()
    assert fresh["cost_by_model"] == []          # fresh=1 sees the empty DB


# ---------------------------------------------------------------- heatmap

def _insert_tz_probe_rows():
    """Two records with a unique model, one in winter (CET, UTC+1) and one
    in summer (CEST, UTC+2), to prove the endpoint is DST-aware."""
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn, conn.cursor() as cur:
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


def test_activity_heatmap_dst_awareness(app_with_data):
    _insert_tz_probe_rows()
    r = app_with_data.get("/api/activity-heatmap?range=3650d&model=tz-probe-model")
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
