"""parse.py — per-file extraction.

Each parse_file call returns:
  - records: one entry per assistant_usage AFTER within-file requestId
    max-merge. Cross-file uuid dedup happens at query time, not here.
  - ctx_turns: per-turn (idx, ts, line, input, output, delta) array
    matching parse_session.py:compute_context_growth.

Cost is precomputed per record using pricing.MODEL_RATES so the read
path doesn't need to JOIN against rates.
"""
from pathlib import Path

import pytest

from backend import parse


FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def _read(name):
    return (FIX / name).read_bytes()


def test_single_turn_emits_one_record_one_turn():
    out = parse.parse_file("k/sess-1/sess-1.jsonl", _read("single_turn.jsonl"))
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r["uuid"] == "a1"
    assert r["request_id"] == "req-1"
    assert r["model"] == "claude-sonnet-4-5"
    assert r["fresh_tokens"] == 100
    assert r["output_tokens"] == 200
    assert r["cost_usd"] == pytest.approx(100/1e6 * 3.00 + 200/1e6 * 15.00)
    assert len(out["ctx_turns"]) == 1
    t = out["ctx_turns"][0]
    assert t["idx"] == 1
    assert t["input"] == 100
    assert t["output"] == 200
    assert t["delta"] == 100   # first turn delta == input


def test_streaming_within_file_max_merges_per_request_id():
    """Two records same requestId, output_tokens=50 then 200.
    Phase 1 keeps ONE record with max-merged usage."""
    out = parse.parse_file(
        "k/sess-2/sess-2.jsonl", _read("streaming_merge.jsonl")
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["output_tokens"] == 200
    assert out["records"][0]["fresh_tokens"] == 100


def test_unsplit_cache_charged_at_5m_rate():
    out = parse.parse_file(
        "k/sess-5/sess-5.jsonl", _read("unsplit_cache.jsonl")
    )
    r = out["records"][0]
    # cache_creation_tokens=1M, eph5=0, eph1h=0 → unsplit=1M → cost via 5m rate
    # Sonnet-4-5 5m rate is $3.75/MTok → expect $3.75
    assert r["cache_creation_tokens"] == 1_000_000
    assert r["eph5_tokens"] == 0
    assert r["eph1h_tokens"] == 0
    assert r["cost_usd"] == pytest.approx(3.75, rel=1e-9)


def test_ttl_split_charges_each_bucket():
    out = parse.parse_file(
        "k/sess-6/sess-6.jsonl", _read("ttl_split.jsonl")
    )
    r = out["records"][0]
    # eph5=1M @ $3.75 + eph1h=1M @ $6.00 = $9.75
    assert r["eph5_tokens"] == 1_000_000
    assert r["eph1h_tokens"] == 1_000_000
    assert r["cost_usd"] == pytest.approx(9.75, rel=1e-9)


def test_ctx_turns_match_canonical_shape():
    """Each turn is {idx, ts, line, input, output, delta} where:
      input  = fresh + cache_creation + cache_read
      output = output_tokens
      delta  = this_input - previous_input
    Mirrors parse_session.py:compute_context_growth lines 2680-2740."""
    out = parse.parse_file(
        "k/sess-1/sess-1.jsonl", _read("single_turn.jsonl")
    )
    t = out["ctx_turns"][0]
    assert set(t.keys()) == {"idx", "ts", "line", "input", "output", "delta"}


def test_legacy_record_no_uuid_no_request_id():
    """Record with no uuid and no requestId still gets stored
    (Phase 1 dedup is by requestId, so empty requestId means each
    record stays distinct)."""
    blob = (
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:01Z",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":10,"output_tokens":5,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    out = parse.parse_file("k/sess-x/sess-x.jsonl", blob)
    assert len(out["records"]) == 1
    assert out["records"][0]["uuid"] is None
    assert out["records"][0]["request_id"] == ""


def test_two_records_no_request_id_both_kept():
    """Without requestId the per-file Phase 1 merge can't dedup, so two
    records stay as two records."""
    blob = (
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:01Z","uuid":"a",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":10,"output_tokens":5,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:02Z","uuid":"b",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":10,"output_tokens":5,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    out = parse.parse_file("k/sess-x/sess-x.jsonl", blob)
    assert len(out["records"]) == 2


def test_tool_use_matched_to_error_result():
    """A tool_use with a later tool_result is_error:true on the same
    tool_use_id → tool_uses entry has is_error=True."""
    out = parse.parse_file(
        "k/sess-err/sess-err.jsonl", _read("tool_error.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    tu = out["tool_uses"][0]
    assert tu["tool_name"] == "Bash"
    assert tu["is_error"] is True


def test_tool_use_matched_to_success_result():
    """A tool_use with a later tool_result is_error:false → is_error=False."""
    out = parse.parse_file(
        "k/sess-ok/sess-ok.jsonl", _read("tool_success.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    assert out["tool_uses"][0]["is_error"] is False


def test_tool_use_unmatched_stays_null():
    """A tool_use with NO later tool_result in the file → is_error=None."""
    out = parse.parse_file(
        "k/sess-pending/sess-pending.jsonl", _read("tool_unmatched.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    assert out["tool_uses"][0]["is_error"] is None


def test_iterations_flattened_to_sum():
    """Multi-iteration usage: top-level fields are partial snapshots.
    Parser must sum across iterations for billing tokens."""
    out = parse.parse_file(
        "k/sess-it/sess-it.jsonl", _read("iterations_flatten.jsonl")
    )
    assert len(out["records"]) == 1
    r = out["records"][0]
    # Top-level in fixture: fresh=2, create=3690, read=232289, output=292
    # Sum across iterations: fresh=118735, create=3690, read=232289, output=5396
    assert r["fresh_tokens"] == 118735
    assert r["cache_creation_tokens"] == 3690
    assert r["cache_read_tokens"] == 232289
    assert r["output_tokens"] == 5396
    # eph1h = 2623 + 1067 = 3690, eph5 = 0
    assert r["eph1h_tokens"] == 3690
    assert r["eph5_tokens"] == 0


def test_prompt_count_excludes_instrumentation_and_interrupts():
    """prompt_count tracks substantive user text only — bash-IO blobs,
    command stubs, and interrupt markers don't count as prompts."""
    blob = (
        b'{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1",'
        b'"message":{"role":"user","content":"real prompt"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:01Z","uuid":"u2",'
        b'"message":{"role":"user","content":"<bash-input>ls</bash-input>"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:02Z","uuid":"u3",'
        b'"message":{"role":"user","content":"<command-name>foo</command-name>"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:03Z","uuid":"u4",'
        b'"message":{"role":"user","content":"[Request interrupted by user]"}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:04Z","uuid":"u5",'
        b'"message":{"role":"user","content":"another real prompt"}}\n'
    )
    out = parse.parse_file("k/sess-p/sess-p.jsonl", blob)
    assert out["prompt_count"] == 2


def test_user_text_lines_drive_turn_boundaries():
    """compute_context_growth uses user_text lines as turn boundaries.
    Within one turn (between two user_text lines), the LAST
    assistant_usage is the turn's representative.
    Build a fixture: 1 user msg → 2 assistant_usage → 1 user msg →
    1 assistant_usage. Expect 2 ctx_turns."""
    blob = (
        b'{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1",'
        b'"message":{"role":"user","content":"first"}}\n'
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:01Z","uuid":"a1",'
        b'"requestId":"r1",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":50,"output_tokens":1,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:02Z","uuid":"a2",'
        b'"requestId":"r2",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":100,"output_tokens":2,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
        b'{"type":"user","timestamp":"2026-05-07T10:00:03Z","uuid":"u2",'
        b'"message":{"role":"user","content":"second"}}\n'
        b'{"type":"assistant","timestamp":"2026-05-07T10:00:04Z","uuid":"a3",'
        b'"requestId":"r3",'
        b'"message":{"role":"assistant","model":"claude-sonnet-4-5",'
        b'"content":[{"type":"text","text":"x"}],'
        b'"usage":{"input_tokens":200,"output_tokens":3,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n'
    )
    out = parse.parse_file("k/sess-x/sess-x.jsonl", blob)
    # Two user_text lines → two turns. Each turn's representative is the
    # LAST assistant_usage before the next user line.
    assert len(out["ctx_turns"]) == 2
    assert out["ctx_turns"][0]["input"] == 100  # a2 wins turn 1
    assert out["ctx_turns"][0]["output"] == 2
    assert out["ctx_turns"][1]["input"] == 200  # a3 wins turn 2
    assert out["ctx_turns"][1]["delta"] == 100  # 200 - 100


def test_reply_latency_terminated_by_list_form_interrupt():
    """List-form interrupt content must terminate the reply-latency window,
    not be ignored. Regression: bogus ~1h latency outlier after interrupts."""
    out = parse.parse_file(
        "k/sess-interrupt/sess-interrupt.jsonl",
        _read("interrupt_list_content.jsonl"),
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["reply_latency_s"] is None
    assert out["prompt_count"] == 1  # interrupt must not count as a prompt


def test_reply_latency_anchored_by_list_form_user_text():
    """Genuine user text in list-form content anchors the latency window
    and counts toward prompt_count."""
    out = parse.parse_file(
        "k/sess-list/sess-list.jsonl",
        _read("list_content_anchor.jsonl"),
    )
    assert len(out["records"]) == 1
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)
    assert out["prompt_count"] == 1


def test_reply_latency_ignores_replayed_prompt():
    """A verbatim re-dispatched user record (same uuid, different line)
    must not anchor reply-latency windows. Regression: bogus ~25m outliers
    on re-dispatched subagent transcripts."""
    out = parse.parse_file(
        "k/sess-rp/sess-rp.jsonl", _read("replayed_prompt.jsonl")
    )
    assert len(out["records"]) == 2
    assert out["records"][0]["reply_latency_s"] == pytest.approx(5.0)
    assert out["records"][1]["reply_latency_s"] is None
    assert out["prompt_count"] == 2


def test_dated_rate_prices_each_record_at_its_own_timestamp():
    # Sonnet 5 introductory pricing runs through 2026-08-31 UTC; a record
    # on either side of the cutover must be priced by when it was spent,
    # not by when the file is parsed.
    out = parse.parse_file("k/sess-d/sess-d.jsonl", _read("dated_rate_sonnet5.jsonl"))
    intro, listed = out["records"]
    assert intro["cost_usd"] == pytest.approx(2.00)
    assert listed["cost_usd"] == pytest.approx(3.00)
