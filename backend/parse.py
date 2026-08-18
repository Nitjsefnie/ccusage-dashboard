"""JSONL → per-file (records list + ctx_turns array).

Each call to parse_file processes ONE jsonl. Within-file Phase 1
requestId max-merge happens here. Cross-file uuid dedup is a
query-time concern (DISTINCT ON (uuid) in the read endpoints).

Cost is precomputed per-record using pricing.MODEL_RATES so the
read path doesn't need to JOIN against rates. Bumps to the rate
table OR to the parse algorithm both require a PARSER_VERSION
bump to invalidate every files row.

Structure: _LineWalk carries the mutable per-file state through the
line-by-line pass (one method per record kind); the module-level
helpers then project the walked events into records and ctx_turns.
"""
from __future__ import annotations

from datetime import datetime, timezone

from orjson import JSONDecodeError, loads

from backend import pricing
from backend.bash_churn import bash_churn


_INSTRUMENTATION_USER_PREFIXES = (
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
)
_INTERRUPT_MARKER = "[Request interrupted by user"


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


def _usage_ctx_input(u: dict) -> int:
    # Per-call context-window size. Mirrors parse_session.py 1.20.6:
    # when the harness fans out multiple sub-calls (advisor()/retries),
    # they get rolled into one `usage` envelope as `iterations`. The
    # top-level fresh+create+read is the BILLING sum across iterations,
    # not the peak single-call window. For context-growth panels we
    # want the peak, so take max-of-iteration-totals when >1 iters.
    # Single-iter (or absent) → fall back to top-level sum.
    iters = u.get("iterations") or []
    if isinstance(iters, list) and len(iters) > 1:
        return max(
            (int(it.get("input_tokens", 0) or 0)
             + int(it.get("cache_creation_input_tokens", 0) or 0)
             + int(it.get("cache_read_input_tokens", 0) or 0))
            for it in iters
        )
    return (
        int(u.get("input_tokens", 0) or 0)
        + int(u.get("cache_creation_input_tokens", 0) or 0)
        + int(u.get("cache_read_input_tokens", 0) or 0)
    )


def _flatten_usage(usage: dict) -> dict:
    """Flatten a usage dict by summing iterations[] into top-level fields.

    Anthropic's API returns multi-turn responses (e.g. advisor_message
    iterations) as an ``iterations`` array. The top-level ``input_tokens``,
    ``output_tokens``, etc. in these records are partial snapshots — only
    the sum across iterations reflects the true token spend.
    """
    iters = usage.get("iterations")
    if not iters:
        return usage
    out = dict(usage)
    int_keys = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    )
    nested_keys = ("cache_creation", "server_tool_use")
    for k in int_keys:
        out[k] = sum((i.get(k) or 0) for i in iters)
    for k in nested_keys:
        merged: dict[str, int] = {}
        for i in iters:
            nested = i.get(k)
            if not isinstance(nested, dict):
                continue
            for nk, nv in nested.items():
                if isinstance(nv, int):
                    merged[nk] = merged.get(nk, 0) + nv
        if merged:
            out[k] = merged
    return out


def _line_count(s: str) -> int:
    """Lines in a payload string, git-style: "a\nb\n" and "a\nb" are both
    2 lines; "" is 0."""
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _tool_churn(name: str, tool_input: dict) -> tuple[int, int]:
    """(lines_added, lines_deleted) for one edit/write tool CALL.

    Derived from the call ARGUMENTS, not the tool result: the result's
    text is a bare confirmation ("The file ... has been updated
    successfully"), and while ~80% of results also carry a structured
    toolUseResult diff, 20% carry none — the call args are the one
    source present on every call (verified over the full corpus:
    27,045 Edit + 8,365 Write, all with old/new payloads). Two
    separate POSITIVE series per issue #10 — deletions are NOT
    encoded as negative additions.

    Edit, Write and Bash are the names handled (no MultiEdit /
    NotebookEdit rows exist in the corpus at all). Bash carries the
    bulk of it: under bypass permissions the model edits through the
    shell, so `backend.bash_churn` recovers what the command text
    enumerates directly — heredoc bodies written to a file, inline
    patch hunks, literal python replacements — and 0 for everything
    that would need the command to be RUN to know.
    Known approximations: an Edit with replace_all=true is counted
    once (the call does not say how many occurrences exist), and a
    Write overwriting an existing file counts its old content as 0
    deletions (the call does not carry it).
    """
    if name == "Edit":
        return (
            _line_count(str(tool_input.get("new_string", "") or "")),
            _line_count(str(tool_input.get("old_string", "") or "")),
        )
    if name == "Write":
        return (
            _line_count(str(tool_input.get("content", "") or "")),
            0,
        )
    if name == "Bash":
        return bash_churn(str(tool_input.get("command", "") or ""))
    return 0, 0


def _to_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _content_metrics(msg: dict) -> tuple[int, list]:
    """(text_chars, tool_use blocks) for one assistant message.

    Visible-response size: sum character lengths of `text` blocks
    in the assistant message. Per analyst (2026-05-07), thinking
    tokens roll into output_tokens undifferentiated, so token-based
    response-size metrics conflate "size" with "how much the model
    thought". Character count of text content blocks is the clean
    measure of visible response size.
    Also extract every `tool_use` block — the `name` field is what
    the canonical parser exposes via --tools. Stored later as one
    row per tool call in the `tool_uses` table for the per-tool
    ratio panel.
    """
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
            elif btype in ("tool_use", "server_tool_use"):
                # server_tool_use = model invoked an Anthropic-hosted
                # tool (e.g. WebSearch). Same shape as tool_use; treat
                # both as tool calls in the panel.
                name = str(blk.get("name", "") or "")
                if name:
                    added, deleted = _tool_churn(
                        name, blk.get("input") or {}
                    )
                    msg_tool_uses.append({
                        "idx": idx,
                        "tool_name": name,
                        "tool_use_id": str(blk.get("id", "") or ""),
                        "lines_added": added,
                        "lines_deleted": deleted,
                    })
    elif isinstance(msg_content, str):
        text_chars = len(msg_content)
    return text_chars, msg_tool_uses


class _LineWalk:
    """Mutable per-file state for the line-by-line pass."""

    def __init__(self, file_key: str) -> None:
        self.file_key = file_key
        self.seen_request: dict[str, dict] = {}
        self.records_in_order: list[dict] = []
        self.user_text_lines: list[int] = []
        self.rate_limit_hits: list[dict] = []
        self.tool_uses: list[dict] = []
        self.seen_tool_ids: set[str] = set()
        # Per-file map of assistant tool_use.id -> bool(is_error).
        # Populated from later user tool_result blocks; consumed after
        # the line walk to fill tool_uses[*]["is_error"].
        self.tool_result_is_error: dict[str, bool] = {}
        # Reply-latency anchor: last NON-instrumentation, NON-interrupt user
        # message timestamp. Cleared when consumed by an assistant message,
        # an interrupt marker, or superseded by a fresher user message.
        # Mirrors compute_reply_latency() in parse_session.py at the
        # message granularity (we treat the assistant message line as the
        # terminator since all assistant content blocks share its ts).
        self.last_user_ts: datetime | None = None
        # Per-file first-seen line for each user-record uuid. A user record
        # whose uuid already appeared on an EARLIER, DIFFERENT line is a
        # replay and is invisible to reply-latency anchoring/superseding.
        self.seen_user_uuids: dict[str, int] = {}

    def handle_user_text(
        self, text: str, line_num: int, ts_str: str, mutate_anchor: bool = True
    ) -> None:
        """Apply instrumentation/interrupt/anchor logic to a user text string.

        Mirrors the string-content branch for list-form text blocks.
        Replayed user records pass ``mutate_anchor=False`` so they still
        count toward ``prompt_count`` and ctx-turn boundaries but never
        touch the latency anchor.
        """
        if not text.strip():
            return
        stripped = text.lstrip()
        if any(stripped.startswith(p) for p in _INSTRUMENTATION_USER_PREFIXES):
            return
        if stripped.startswith(_INTERRUPT_MARKER):
            if mutate_anchor:
                self.last_user_ts = None
            return
        self.user_text_lines.append(line_num)
        ts_dt = _to_dt(ts_str)
        if ts_dt is not None and mutate_anchor:
            self.last_user_ts = ts_dt

    def handle_rate_limit(self, obj: dict, line_num: int) -> bool:
        """Book a rate-limit hit if this record is one. Returns True when
        the record was consumed — a rate-limit error record carries no
        usage, so there is nothing else to do with it.

        Detection (per analyst, 2026-05-07): hits live on type:"assistant"
        records with isApiErrorMessage=True and error="rate_limit", and
        the message text contains "out of extra usage". Per-minute API
        429s also have error="rate_limit" but say "Server is temporarily
        limiting requests" — those are ignored (text-match on "out of
        extra usage" is the reliable signal).
        """
        if not (
            obj.get("type", "") == "assistant"
            and obj.get("isApiErrorMessage") is True
            and obj.get("error") == "rate_limit"
        ):
            return False
        content_list = (obj.get("message") or {}).get("content") or []
        joined = " ".join(
            str(c.get("text", ""))
            for c in content_list
            if isinstance(c, dict) and c.get("type") == "text"
        )
        if "out of extra usage" in joined.lower():
            self.rate_limit_hits.append({
                "line": line_num,
                "ts": obj.get("timestamp", "") or "",
                "content": joined[:500],
            })
        return True

    def handle_user_line(self, obj: dict, msg: dict, line_num: int) -> None:
        """One user-role record: replay detection, then the content walk."""
        uuid = obj.get("uuid") or ""
        is_replay = False
        if uuid:
            first_line = self.seen_user_uuids.get(uuid)
            if first_line is None:
                self.seen_user_uuids[uuid] = line_num
            elif first_line != line_num:
                is_replay = True
        mutate_anchor = not is_replay

        content = msg.get("content")
        ts_str = obj.get("timestamp", "") or ""
        if isinstance(content, str) and content.strip():
            self.handle_user_text(
                content, line_num, ts_str, mutate_anchor=mutate_anchor
            )
        elif isinstance(content, list):
            for blk in content:
                self._handle_user_block(blk, line_num, ts_str, mutate_anchor)

    def _handle_user_block(self, blk, line_num: int, ts_str: str,
                           mutate_anchor: bool) -> None:
        if not isinstance(blk, dict):
            return
        btype = blk.get("type")
        if btype == "tool_result":
            # Tool results live here. Each block carries
            # tool_use_id (referencing the assistant's
            # tool_use.id) and an optional is_error flag.
            tu_id = blk.get("tool_use_id")
            if not tu_id:
                return
            self.tool_result_is_error[str(tu_id)] = bool(
                blk.get("is_error", False)
            )
        elif btype == "text":
            text = blk.get("text", "") or ""
            if isinstance(text, str) and text.strip():
                self.handle_user_text(
                    text, line_num, ts_str, mutate_anchor=mutate_anchor
                )

    def handle_assistant_line(self, obj: dict, msg: dict,
                              line_num: int) -> None:
        """One usage-bearing assistant record: metrics, Phase 1 merge,
        tool calls."""
        usage = msg.get("usage")
        if not usage:
            return
        usage = _flatten_usage(usage)
        # Skip synthetic stubs: Claude Code emits these after `/exit`
        # (text='No response requested.') and for interrupted partial
        # responses. They have all-zero usage and no requestId, but
        # they sit at the end of the file — without this filter, the
        # ctx_turns walk picks them as the trailing `last_usage`,
        # then the post-walk input>0 filter drops them, leaving
        # ctx_turns empty even when real records preceded them.
        # (Mirrors parse_session.py 1.20.4 fix from analyst 2026-05-07.)
        if (msg.get("model") or "") == "<synthetic>":
            return

        text_chars, msg_tool_uses = _content_metrics(msg)
        req_id = obj.get("requestId", "") or ""
        ev = {
            "line_num": line_num,
            "uuid": obj.get("uuid") or None,
            "request_id": req_id,
            "ts": obj.get("timestamp", "") or "",
            "model": msg.get("model") or "(unknown)",
            "usage": dict(usage),
            "text_chars": text_chars,
            "reply_latency_s": self._consume_anchor(obj),
        }
        if req_id and req_id in self.seen_request:
            existing = self.seen_request[req_id]
            existing["usage"] = _merge_usage_max(existing["usage"], usage)
            # Same Phase 1 max-merge for text_chars: streaming responses
            # log incrementally; the largest sample is the final size.
            if text_chars > existing.get("text_chars", 0):
                existing["text_chars"] = text_chars
        else:
            if req_id:
                self.seen_request[req_id] = ev
            self.records_in_order.append(ev)

        self._record_tool_uses(
            msg_tool_uses, req_id, line_num, obj.get("timestamp", "") or ""
        )

    def _consume_anchor(self, obj: dict) -> float | None:
        """Reply latency: gap from last anchored user-message ts to
        this assistant message's ts. NULL when there's no preceding
        anchored user message (session start, or every recent user
        msg was instrumentation/interrupt) OR when delta is
        negative (analyst 2026-05-07: negative deltas are
        session-restore / compaction replay artifacts where the
        assistant message came from a prior, re-emitted state and
        isn't actually a reply to the visually-preceding user msg).
        Clamping to 0 would pollute the p0/p50 distribution; drop
        the measurement instead."""
        reply_latency_s = None
        if self.last_user_ts is not None:
            assistant_dt = _to_dt(obj.get("timestamp", "") or "")
            if assistant_dt is not None:
                delta_s = (assistant_dt - self.last_user_ts).total_seconds()
                if delta_s >= 0:
                    reply_latency_s = delta_s
        self.last_user_ts = None  # anchor consumed by this assistant reply
        return reply_latency_s

    def _record_tool_uses(self, msg_tool_uses: list, req_id: str,
                          line_num: int, ts_str: str) -> None:
        # Tool calls: dedupe on tool_use.id, NOT on first-line-of-request.
        # Claude Code writes one JSONL line per content block, so a turn's
        # tool_use blocks land on LATER lines of the same requestId — and
        # recording them only on the first line dropped every one of those.
        # Measured on live transcripts: 57-71% of all tool_use blocks sit on
        # a later line. The premise this replaces ("streaming dupes carry the
        # same tool_use blocks") does not hold — in those same transcripts
        # every block id was distinct (520/520, 545/545, 401/401), so there
        # were no dupes to suppress. The id is globally unique, so keying on
        # it still kills any genuine repeat while keeping every real call.
        # Idless blocks fall back to (req, line, idx).
        for tu in msg_tool_uses:
            tu_key = tu["tool_use_id"] or f"{req_id}:{line_num}:{tu['idx']}"
            if tu_key in self.seen_tool_ids:
                continue
            self.seen_tool_ids.add(tu_key)
            self.tool_uses.append({
                "file_key": self.file_key,
                "line_num": line_num,
                "idx": tu["idx"],
                "ts": _to_dt(ts_str),
                "tool_name": tu["tool_name"],
                "tool_use_id": tu["tool_use_id"],
                "is_error": None,  # filled after the line walk
                "lines_added": tu["lines_added"],
                "lines_deleted": tu["lines_deleted"],
            })


def _resolve_tool_errors(tool_uses: list, tool_result_is_error: dict) -> None:
    """Resolve tool_result.is_error onto each tool_uses entry by
    tool_use_id. Unmatched entries keep is_error=None and are
    excluded from rate denominators at query time. An errored call
    changed nothing on disk, so its churn is zeroed (an unmatched
    call keeps its churn: the file may simply end before the
    result record)."""
    for tu in tool_uses:
        tu_id = tu.pop("tool_use_id", "")
        if tu_id and tu_id in tool_result_is_error:
            tu["is_error"] = tool_result_is_error[tu_id]
            if tu["is_error"]:
                tu["lines_added"] = 0
                tu["lines_deleted"] = 0


def _project_record(file_key: str, ev: dict) -> dict:
    """One walked event → its records-table row (token columns + cost)."""
    u = ev["usage"]
    fresh = int(u.get("input_tokens", 0) or 0)
    create = int(u.get("cache_creation_input_tokens", 0) or 0)
    read = int(u.get("cache_read_input_tokens", 0) or 0)
    output = int(u.get("output_tokens", 0) or 0)
    eph = u.get("cache_creation") or {}
    eph5 = int(eph.get("ephemeral_5m_input_tokens", 0) or 0)
    eph1h = int(eph.get("ephemeral_1h_input_tokens", 0) or 0)
    unsplit = max(0, create - eph5 - eph1h)
    ts = _to_dt(ev["ts"])
    cost = pricing.compute_cost(
        ev["model"],
        fresh=fresh, output=output,
        eph5=eph5, eph1h=eph1h,
        unsplit_create=unsplit, read=read,
        # Dated rates apply to when the tokens were spent, not to
        # when this file happens to be parsed.
        ts=ts,
    )
    return {
        "file_key": file_key,
        "line_num": ev["line_num"],
        "uuid": ev["uuid"],
        "request_id": ev["request_id"],
        "ts": ts,
        "model": ev["model"],
        "fresh_tokens": fresh,
        "cache_creation_tokens": create,
        "cache_read_tokens": read,
        "output_tokens": output,
        "text_chars": int(ev.get("text_chars", 0)),
        "reply_latency_s": ev.get("reply_latency_s"),
        "eph5_tokens": eph5,
        "eph1h_tokens": eph1h,
        "cost_usd": round(cost, 6),
        "ctx_input": _usage_ctx_input(u),
    }


def _build_ctx_turns(records: list, user_text_lines: list) -> list:
    """Build ctx_turns by user-text boundary, mirroring
    parse_session.py:compute_context_growth lines 2680-2740."""
    boundary_lines = sorted(user_text_lines)
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    sorted_recs = sorted(
        records,
        key=lambda r: (
            r["ts"] is not None,
            r["ts"] if r["ts"] is not None else aware_min,
            r["line_num"],
        ),
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
    turn_records = [t for t in turn_records if t["ctx_input"] > 0]

    ctx_turns: list[dict] = []
    prev_input = 0
    for idx, t in enumerate(turn_records, 1):
        ctx_input = t["ctx_input"]
        ctx_turns.append({
            "idx": idx,
            "ts": t["ts"].isoformat() if t["ts"] else "",
            "line": t["line_num"],
            "input": ctx_input,
            "output": t["output_tokens"],
            "delta": ctx_input - prev_input,
        })
        prev_input = ctx_input
    return ctx_turns


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
    walk = _LineWalk(file_key)
    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue

        if obj.get("type", "") not in ("user", "assistant"):
            continue
        if walk.handle_rate_limit(obj, line_num):
            continue

        msg = obj.get("message") or {}
        role = msg.get("role")
        if role == "user":
            walk.handle_user_line(obj, msg, line_num)
        elif role == "assistant":
            walk.handle_assistant_line(obj, msg, line_num)

    _resolve_tool_errors(walk.tool_uses, walk.tool_result_is_error)
    records = [
        _project_record(file_key, ev) for ev in walk.records_in_order
    ]
    ctx_turns = _build_ctx_turns(records, walk.user_text_lines)

    return {
        "records": records,
        "ctx_turns": ctx_turns,
        "turn_count": len(ctx_turns),
        # Substantive user text messages — instrumentation
        # (bash IO, command stubs) and interrupt markers excluded.
        # `turn_count` only counts prompts that produced a usage-bearing
        # assistant reply; `prompt_count` is the raw "how many times did
        # the user actually type something" total. Prompts ≥ Turns.
        "prompt_count": len(walk.user_text_lines),
        "rate_limit_hits": walk.rate_limit_hits,
        "tool_uses": walk.tool_uses,
    }
