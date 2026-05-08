import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_data(monkeypatch):
    """Spin up a fresh DB + mini R2, ingest, return an authed TestClient.

    Bypasses auth via a clean FastAPI app with only the api router.
    """
    test_db = "claude_viz_test_api"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f /root/session-viz/backend/schema.sql >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = Path("/root/session-viz/fixtures/r2_mini")
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
