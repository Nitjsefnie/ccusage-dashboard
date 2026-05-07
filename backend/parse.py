"""JSONL → per-file (records list + ctx_turns array).

Each call to parse_file processes ONE jsonl. Within-file Phase 1
requestId max-merge happens here. Cross-file uuid dedup is a
query-time concern (DISTINCT ON (uuid) in the read endpoints).

Cost is precomputed per-record using pricing.MODEL_RATES so the
read path doesn't need to JOIN against rates. Bumps to the rate
table OR to the parse algorithm both require a PARSER_VERSION
bump to invalidate every files row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import orjson

from backend import pricing


def _merge_usage_max(existing, incoming):
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    if isinstance(existing, (int, float)) and isinstance(incoming, (int, float)):
        return max(existing, incoming)
    if isinstance(existing, dict) and isinstance(incoming, dict):
        out = dict(existing)
        for k, v in incoming.items():
            out[k] = _merge_usage_max(out.get(k), v) if k in out else v
        return out
    return existing


def _to_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_file(file_key: str, blob: bytes) -> dict:
    """Parse one jsonl. Returns {records, ctx_turns, turn_count, rate_limit_hits}.

    records: list of dicts with keys
      file_key, line_num, uuid, request_id, ts (datetime|None), model,
      fresh_tokens, cache_creation_tokens, cache_read_tokens,
      output_tokens, eph5_tokens, eph1h_tokens, cost_usd

    ctx_turns: list of dicts with keys
      idx, ts, line, input, output, delta

    rate_limit_hits: list of dicts with keys
      line, ts (string ISO), content
    Detected by mirroring src/parser.js: any `type:"system"` line whose
    content+subtype lower-cased contains "rate limit", "rate_limit", or
    "429".

    records + ctx_turns are AFTER Phase 1 within-file requestId max-merge.
    Records WITHOUT a request_id are NOT dedup'd (each kept distinct).
    """
    seen_request: dict[str, dict] = {}
    records_in_order: list[dict] = []
    user_text_lines: list[int] = []
    rate_limit_hits: list[dict] = []
    tool_uses: list[dict] = []

    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue

        msg_type = obj.get("type", "")
        if msg_type not in ("user", "assistant"):
            continue

        # Rate-limit-hit detection (per analyst, 2026-05-07):
        # Hits live on type:"assistant" records with isApiErrorMessage=True
        # and error="rate_limit", and the message text contains "out of
        # extra usage". Per-minute API 429s also have error="rate_limit"
        # but say "Server is temporarily limiting requests" — those are
        # ignored (text-match on "out of extra usage" is the reliable signal).
        if (
            msg_type == "assistant"
            and obj.get("isApiErrorMessage") is True
            and obj.get("error") == "rate_limit"
        ):
            content_list = (obj.get("message") or {}).get("content") or []
            joined = " ".join(
                str(c.get("text", ""))
                for c in content_list
                if isinstance(c, dict) and c.get("type") == "text"
            )
            if "out of extra usage" in joined.lower():
                rate_limit_hits.append({
                    "line": line_num,
                    "ts": obj.get("timestamp", "") or "",
                    "content": joined[:500],
                })
            # rate-limit error records carry no usage; nothing else to do
            continue

        msg = obj.get("message") or {}
        role = msg.get("role")
        if role == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                user_text_lines.append(line_num)
            continue

        if role != "assistant":
            continue
        usage = msg.get("usage")
        if not usage:
            continue

        # Visible-response size: sum character lengths of `text` blocks
        # in the assistant message. Per analyst (2026-05-07), thinking
        # tokens roll into output_tokens undifferentiated, so token-based
        # response-size metrics conflate "size" with "how much the model
        # thought". Character count of text content blocks is the clean
        # measure of visible response size.
        # Also extract every `tool_use` block — the `name` field is what
        # the canonical parser exposes via --tools. Stored later as one
        # row per tool call in the `tool_uses` table for the per-tool
        # ratio panel.
        msg_content = msg.get("content")
        text_chars = 0
        msg_tool_uses: list[dict] = []
        if isinstance(msg_content, list):
            for idx, blk in enumerate(msg_content):
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text":
                    text_chars += len(str(blk.get("text", "")))
                elif btype == "tool_use":
                    name = str(blk.get("name", "") or "")
                    if name:
                        msg_tool_uses.append({"idx": idx, "tool_name": name})
        elif isinstance(msg_content, str):
            text_chars = len(msg_content)

        req_id = obj.get("requestId", "") or ""
        ev = {
            "line_num": line_num,
            "uuid": obj.get("uuid") or None,
            "request_id": req_id,
            "ts": obj.get("timestamp", "") or "",
            "model": msg.get("model") or "(unknown)",
            "usage": dict(usage),
            "text_chars": text_chars,
        }
        if req_id and req_id in seen_request:
            existing = seen_request[req_id]
            existing["usage"] = _merge_usage_max(existing["usage"], usage)
            # Same Phase 1 max-merge for text_chars: streaming responses
            # log incrementally; the largest sample is the final size.
            if text_chars > existing.get("text_chars", 0):
                existing["text_chars"] = text_chars
        else:
            if req_id:
                seen_request[req_id] = ev
            records_in_order.append(ev)
            # Tool calls: record only on the FIRST occurrence of a
            # requestId — streaming dupes carry the same tool_use blocks
            # and shouldn't be double-counted. Captured with the line's
            # ts so the tool-ratio panel can bucket by time.
            for tu in msg_tool_uses:
                tool_uses.append({
                    "file_key": file_key,
                    "line_num": line_num,
                    "idx": tu["idx"],
                    "ts": _to_dt(obj.get("timestamp", "") or ""),
                    "tool_name": tu["tool_name"],
                })

    # Project usage → token columns + cost; drop the raw 'usage' dict.
    records: list[dict] = []
    for ev in records_in_order:
        u = ev["usage"]
        fresh = int(u.get("input_tokens", 0) or 0)
        create = int(u.get("cache_creation_input_tokens", 0) or 0)
        read = int(u.get("cache_read_input_tokens", 0) or 0)
        output = int(u.get("output_tokens", 0) or 0)
        eph = u.get("cache_creation") or {}
        eph5 = int(eph.get("ephemeral_5m_input_tokens", 0) or 0)
        eph1h = int(eph.get("ephemeral_1h_input_tokens", 0) or 0)
        unsplit = max(0, create - eph5 - eph1h)
        cost = pricing.compute_cost(
            ev["model"],
            fresh=fresh, output=output,
            eph5=eph5, eph1h=eph1h,
            unsplit_create=unsplit, read=read,
        )
        records.append({
            "file_key": file_key,
            "line_num": ev["line_num"],
            "uuid": ev["uuid"],
            "request_id": ev["request_id"],
            "ts": _to_dt(ev["ts"]),
            "model": ev["model"],
            "fresh_tokens": fresh,
            "cache_creation_tokens": create,
            "cache_read_tokens": read,
            "output_tokens": output,
            "text_chars": int(ev.get("text_chars", 0)),
            "eph5_tokens": eph5,
            "eph1h_tokens": eph1h,
            "cost_usd": round(cost, 6),
        })

    # Build ctx_turns by user-text boundary, mirroring
    # parse_session.py:compute_context_growth lines 2680-2740.
    boundary_lines = sorted(user_text_lines)
    sorted_recs = sorted(
        records, key=lambda r: (r["ts"] or datetime.min, r["line_num"])
    )

    turn_records: list[dict] = []
    last_usage: dict | None = None
    bi = 0
    for rec in sorted_recs:
        while bi < len(boundary_lines) and boundary_lines[bi] <= rec["line_num"]:
            if last_usage is not None:
                turn_records.append(last_usage)
                last_usage = None
            bi += 1
        last_usage = rec
    if last_usage is not None:
        turn_records.append(last_usage)

    # Drop turns with 0 input (refusals/interrupts; they corrupt deltas).
    turn_records = [
        t for t in turn_records
        if (t["fresh_tokens"] + t["cache_creation_tokens"] + t["cache_read_tokens"]) > 0
    ]

    ctx_turns: list[dict] = []
    prev_input = 0
    for idx, t in enumerate(turn_records, 1):
        ctx_input = t["fresh_tokens"] + t["cache_creation_tokens"] + t["cache_read_tokens"]
        ctx_turns.append({
            "idx": idx,
            "ts": t["ts"].isoformat() if t["ts"] else "",
            "line": t["line_num"],
            "input": ctx_input,
            "output": t["output_tokens"],
            "delta": ctx_input - prev_input,
        })
        prev_input = ctx_input

    return {
        "records": records,
        "ctx_turns": ctx_turns,
        "turn_count": len(ctx_turns),
        "rate_limit_hits": rate_limit_hits,
        "tool_uses": tool_uses,
    }
