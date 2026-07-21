# Tool Error Rate Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-model tool-error-rate dashboard panel showing an EMA progression over time, with an "Aggregate" line plus per-tool lines (top-3 default ON), each toggleable via checkboxes.

**Architecture:** A new nullable `is_error BOOLEAN` column on `tool_uses` is filled at parse time by matching assistant `tool_use.id` to user `tool_result.tool_use_id` within each JSONL file. A new `/api/tool-error-rate` endpoint serves adaptively-bucketed `(model, tool, n_total, n_error)` rows. A new `ToolErrorRatePanel` React component (in `src/dashboard-charts-extra.jsx`) renders one sub-panel per model in rows of 2 — same layout pattern as the existing `ContextGrowthPanel` — with EMA(α=0.15) polylines per visible series.

**Tech Stack:** Python 3 / FastAPI / psycopg / orjson backend, React + in-browser Babel frontend, Postgres, pytest.

**Spec:** `docs/superpowers/specs/2026-05-08-tool-error-rate-design.md`

---

## File Map

**Create:**
- `fixtures/parser/tool_error.jsonl` — single tool call with `is_error:true` result.
- `fixtures/parser/tool_success.jsonl` — single tool call with `is_error:false` result.
- `fixtures/parser/tool_unmatched.jsonl` — tool call with no matching result.

**Modify:**
- `backend/schema.sql` — add `ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS is_error BOOLEAN`.
- `backend/parse.py` — capture `tool_use.id`, walk user-message list-content for `tool_result` blocks, fill `is_error` on tool_uses entries.
- `backend/ingest.py` — include `is_error` in the `tool_uses` INSERT.
- `backend/api.py` — add `GET /api/tool-error-rate` endpoint.
- `.env` — bump `PARSER_VERSION` from `7` to `8`.
- `src/dashboard-charts-extra.jsx` — add `ToolErrorRatePanel`, expose via `window.ToolErrorRatePanel`.
- `src/app.jsx` — mount panel after the reply-latency panel.
- `tests/test_parse.py` — add tests for the new parser behavior.
- `tests/test_api.py` — add a smoke test for the new endpoint (if a similar pattern exists for siblings).

---

## Task 1: Schema migration

**Files:**
- Modify: `backend/schema.sql`

- [ ] **Step 1: Add ALTER TABLE for `is_error`**

Append to `backend/schema.sql` immediately after the existing
`tool_uses_tool_idx` index creation (so the column exists before any
indexes that might reference it later):

```sql
-- 2026-05-08: tool_uses.is_error filled at parse time by matching
-- assistant tool_use.id to user tool_result.tool_use_id within the
-- same JSONL file. NULL = unmatched (no later tool_result block in
-- the file). Read endpoints WHERE is_error IS NOT NULL to compute
-- the error rate over settled calls only.
ALTER TABLE tool_uses ADD COLUMN IF NOT EXISTS is_error BOOLEAN;
```

- [ ] **Step 2: Apply schema**

```bash
psql claudit -f backend/schema.sql
```

Expected: no errors. The `ADD COLUMN IF NOT EXISTS` is idempotent.

- [ ] **Step 3: Verify column exists**

```bash
psql claudit -c "\d tool_uses" | grep is_error
```

Expected output: `is_error  | boolean`

- [ ] **Step 4: Commit**

```bash
git add backend/schema.sql
git commit -m "schema: add tool_uses.is_error for error-rate panel"
```

---

## Task 2: Parser — capture tool_use.id and tool_result.is_error

This task adds the in-memory pipeline that links assistant `tool_use`
blocks to user `tool_result` blocks within a single JSONL file. The
output is each `tool_uses` entry gaining an `is_error` field
(`True` / `False` / `None`).

**Files:**
- Create: `fixtures/parser/tool_error.jsonl`
- Create: `fixtures/parser/tool_success.jsonl`
- Create: `fixtures/parser/tool_unmatched.jsonl`
- Modify: `backend/parse.py:243-250` (the `for tu in msg_tool_uses` block) and the surrounding loop.
- Modify: `tests/test_parse.py`

- [ ] **Step 1: Write failing test for an errored tool call**

Add to `tests/test_parse.py`:

```python
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
```

- [ ] **Step 2: Create fixture for the errored tool call**

`fixtures/parser/tool_error.jsonl` (each line is one JSON object — no
trailing whitespace, no blank lines):

```jsonl
{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1","message":{"role":"user","content":"run ls"}}
{"type":"assistant","timestamp":"2026-05-07T10:00:01Z","uuid":"a1","requestId":"req-1","sessionId":"sess-err","message":{"role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"tool_use","id":"toolu_01","name":"Bash","input":{"command":"ls"}}],"usage":{"input_tokens":50,"output_tokens":20,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
{"type":"user","timestamp":"2026-05-07T10:00:02Z","uuid":"u2","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_01","content":"ls: command not found","is_error":true}]}}
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
pytest tests/test_parse.py::test_tool_use_matched_to_error_result -v
```

Expected: FAIL — current parser returns `tool_uses` entries without an
`is_error` key (KeyError or missing assertion).

- [ ] **Step 4: Modify the parser to capture tool_use.id and tool_result.is_error**

In `backend/parse.py`, find the `parse_file` function. Make four
changes:

**4a.** Initialize a per-file result map at the top of the function,
near `tool_uses: list[dict] = []` (around line 84):

```python
    tool_uses: list[dict] = []
    # Per-file map of assistant tool_use.id -> bool(is_error).
    # Populated from later user tool_result blocks; consumed after
    # the line walk to fill tool_uses[*]["is_error"].
    tool_result_is_error: dict[str, bool] = {}
```

**4b.** Inside the user-role branch (around `if role == "user":` on
line 133), extend handling to walk LIST content for `tool_result`
blocks (currently only string content is processed). Replace the
existing `if isinstance(content, str) and content.strip():` block with:

```python
        if role == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                text = content.lstrip()
                # Instrumentation user msgs (bash IO + caveat) are
                # explicitly "DO NOT respond" and never anchor a
                # reply-latency window. Interrupt markers terminate
                # an open window without anchoring a new one. Plain
                # user text supersedes any existing anchor with its ts.
                if any(text.startswith(p) for p in _INSTRUMENTATION_USER_PREFIXES):
                    pass
                elif text.startswith(_INTERRUPT_MARKER):
                    last_user_ts = None
                else:
                    user_text_lines.append(line_num)
                    ts_dt = _to_dt(obj.get("timestamp", "") or "")
                    if ts_dt is not None:
                        last_user_ts = ts_dt
            elif isinstance(content, list):
                # Tool results live here. Each block with type=tool_result
                # carries tool_use_id (referencing the assistant's
                # tool_use.id) and an optional is_error flag.
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") != "tool_result":
                        continue
                    tu_id = blk.get("tool_use_id")
                    if not tu_id:
                        continue
                    tool_result_is_error[str(tu_id)] = bool(blk.get("is_error", False))
            continue
```

**4c.** Inside the assistant tool_use extraction block (around line
189-195), capture the `id` field on each entry:

```python
                elif btype in ("tool_use", "server_tool_use"):
                    # server_tool_use = model invoked an Anthropic-hosted
                    # tool (e.g. WebSearch). Same shape as tool_use; treat
                    # both as tool calls in the panel.
                    name = str(blk.get("name", "") or "")
                    if name:
                        msg_tool_uses.append({
                            "idx": idx,
                            "tool_name": name,
                            "tool_use_id": str(blk.get("id", "") or ""),
                        })
```

**4d.** In the `for tu in msg_tool_uses:` block (around line 243-250),
carry the `tool_use_id` through:

```python
            for tu in msg_tool_uses:
                tool_uses.append({
                    "file_key": file_key,
                    "line_num": line_num,
                    "idx": tu["idx"],
                    "ts": _to_dt(obj.get("timestamp", "") or ""),
                    "tool_name": tu["tool_name"],
                    "tool_use_id": tu["tool_use_id"],
                    "is_error": None,  # filled after the line walk
                })
```

**4e.** After the main `for line_num, raw in enumerate(...)` loop ends
(but before `# Project usage → token columns + cost;` around line 252),
fill `is_error` from the result map:

```python
    # Resolve tool_result.is_error onto each tool_uses entry by
    # tool_use_id. Unmatched entries keep is_error=None and are
    # excluded from rate denominators at query time.
    for tu in tool_uses:
        tu_id = tu.pop("tool_use_id", "")
        if tu_id and tu_id in tool_result_is_error:
            tu["is_error"] = tool_result_is_error[tu_id]
```

- [ ] **Step 5: Re-run the test — should now pass**

```bash
pytest tests/test_parse.py::test_tool_use_matched_to_error_result -v
```

Expected: PASS.

- [ ] **Step 6: Add the success-case test**

Append to `tests/test_parse.py`:

```python
def test_tool_use_matched_to_success_result():
    """A tool_use with a later tool_result is_error:false → is_error=False."""
    out = parse.parse_file(
        "k/sess-ok/sess-ok.jsonl", _read("tool_success.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    assert out["tool_uses"][0]["is_error"] is False
```

- [ ] **Step 7: Create the success fixture**

`fixtures/parser/tool_success.jsonl`:

```jsonl
{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1","message":{"role":"user","content":"list files"}}
{"type":"assistant","timestamp":"2026-05-07T10:00:01Z","uuid":"a1","requestId":"req-2","sessionId":"sess-ok","message":{"role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"tool_use","id":"toolu_02","name":"Bash","input":{"command":"ls"}}],"usage":{"input_tokens":50,"output_tokens":20,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
{"type":"user","timestamp":"2026-05-07T10:00:02Z","uuid":"u2","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_02","content":"file1\nfile2\n","is_error":false}]}}
```

- [ ] **Step 8: Run the success test**

```bash
pytest tests/test_parse.py::test_tool_use_matched_to_success_result -v
```

Expected: PASS.

- [ ] **Step 9: Add the unmatched-case test**

Append to `tests/test_parse.py`:

```python
def test_tool_use_unmatched_stays_null():
    """A tool_use with NO later tool_result in the file → is_error=None."""
    out = parse.parse_file(
        "k/sess-pending/sess-pending.jsonl", _read("tool_unmatched.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    assert out["tool_uses"][0]["is_error"] is None
```

- [ ] **Step 10: Create the unmatched fixture**

`fixtures/parser/tool_unmatched.jsonl`:

```jsonl
{"type":"user","timestamp":"2026-05-07T10:00:00Z","uuid":"u1","message":{"role":"user","content":"run something"}}
{"type":"assistant","timestamp":"2026-05-07T10:00:01Z","uuid":"a1","requestId":"req-3","sessionId":"sess-pending","message":{"role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"tool_use","id":"toolu_03","name":"Bash","input":{"command":"sleep 60"}}],"usage":{"input_tokens":50,"output_tokens":20,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
```

- [ ] **Step 11: Run the unmatched test**

```bash
pytest tests/test_parse.py::test_tool_use_unmatched_stays_null -v
```

Expected: PASS.

- [ ] **Step 12: Run the full parser test suite to confirm no regressions**

```bash
pytest tests/test_parse.py -v
```

Expected: all tests pass (existing tests unaffected — they don't
reference `is_error`).

- [ ] **Step 13: Commit**

```bash
git add backend/parse.py tests/test_parse.py fixtures/parser/tool_error.jsonl fixtures/parser/tool_success.jsonl fixtures/parser/tool_unmatched.jsonl
git commit -m "parse: link tool_use to tool_result.is_error per file"
```

---

## Task 3: Ingest — persist is_error

**Files:**
- Modify: `backend/ingest.py:165-172` (the `INSERT INTO tool_uses` block)

- [ ] **Step 1: Add `is_error` to the INSERT**

In `backend/ingest.py`, replace the existing tool_uses insert (around
line 165-172):

```python
                if parsed.get("tool_uses"):
                    cur.executemany(
                        """
                        INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, is_error)
                        VALUES (%(file_key)s, %(line_num)s, %(idx)s, %(ts)s, %(tool_name)s, %(is_error)s)
                        """,
                        parsed["tool_uses"],
                    )
```

- [ ] **Step 2: Run the ingest tests**

```bash
pytest tests/test_ingest.py -v
```

Expected: all pass. If a test asserts a specific `tool_uses` row shape,
update it to include `is_error` (likely `is_error=None` for the
fixtures used by tests).

- [ ] **Step 3: Commit**

```bash
git add backend/ingest.py
git commit -m "ingest: persist tool_uses.is_error from parser output"
```

---

## Task 4: Bump PARSER_VERSION

**Files:**
- Modify: `.env`

- [ ] **Step 1: Bump version**

In `.env`, change:

```
PARSER_VERSION=7
```

to:

```
PARSER_VERSION=8
```

- [ ] **Step 2: Commit**

```bash
git add .env
git commit -m "bump PARSER_VERSION=8 (tool_uses.is_error)"
```

(Note: `.env` is sometimes gitignored. If `git status` shows it as
ignored, that's fine — the bump still takes effect at the next ingest
run on the deploy host. Just run the commit; if there's nothing to
stage, skip this commit.)

---

## Task 5: API — `/api/tool-error-rate`

**Files:**
- Modify: `backend/api.py` (add new endpoint after `/api/tool-usage`)
- Modify: `tests/test_api.py` (smoke test if pattern exists)

- [ ] **Step 1: Add the endpoint**

In `backend/api.py`, add this function immediately after the existing
`tool_usage` handler (search for `@router.get("/tool-usage")` and add
after the function it decorates):

```python
@router.get("/tool-error-rate")
async def tool_error_rate(
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed (n_total, n_error) per (model, tool_name) over settled
    tool calls only (is_error IS NOT NULL). The frontend computes
    error-rate = n_error / n_total per series and EMA-smooths the
    sequence.

    `model` is an optional model substring filter (parity with
    /api/tool-usage). Cross-file uuid dedup does NOT apply — tool_uses
    aren't keyed on records.uuid; the natural boundary is per-file."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    # Args appended in the order placeholders appear in the SQL string:
    # tu.ts >= %s, then f.project_id, then r.model.
    args: list[Any] = [since]
    proj_filter = ""
    if project:
        proj_filter = "AND f.project_id = %s"
        args.append(project)
    model_filter = ""
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")

    with db.viz_conn() as c:
        rows = c.execute(
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM tu.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   r.model      AS model,
                   tu.tool_name AS tool,
                   COUNT(*)                              AS n_total,
                   COUNT(*) FILTER (WHERE tu.is_error)   AS n_error
            FROM tool_uses tu
            JOIN records r ON r.file_key = tu.file_key AND r.line_num = tu.line_num
            JOIN files   f ON f.file_key = tu.file_key
            WHERE tu.is_error IS NOT NULL
              AND tu.ts >= %s
              {proj_filter}
              {model_filter}
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
            """,
            args,
        ).fetchall()

    return {
        "range": range,
        "project": project,
        "bucket_s": bucket_s,
        "buckets": [
            {"ts": _iso(b), "model": m, "tool": t,
             "n_total": int(nt or 0), "n_error": int(ne or 0)}
            for (b, m, t, nt, ne) in rows
        ],
    }
```

(Args list order matches placeholder order in the final SQL string:
`tu.ts >= %s` first, then optional `f.project_id`, then optional
`r.model LIKE`. Don't reorder these without also reordering the SQL.)

- [ ] **Step 2: Restart the backend and smoke-test the endpoint**

```bash
systemctl restart claudit
curl -sS -b "session=$(cat ~/.claude/claudit.cookie 2>/dev/null || echo '')" \
  'http://127.0.0.1:8000/api/tool-error-rate?range=30d' | python3 -m json.tool | head -30
```

Expected: a JSON object with `range`, `project`, `bucket_s`, `buckets`
keys. `buckets` may be empty until a reparse runs (Task 4 set
`PARSER_VERSION=8`, so the next ingest reparses everything).

If unauthenticated locally, hit the endpoint via guest mode or skip
the auth header — the existing `/api/tool-usage` works the same way,
so reuse whatever cookie/session lets you call that one.

- [ ] **Step 3: Trigger an ingest to populate is_error**

```bash
curl -sS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/admin/ingest
```

Wait for completion. Then re-hit the endpoint — `buckets` should now
be populated (assuming any errored tool calls exist in the ingested
sessions).

- [ ] **Step 4: Add a smoke test for the endpoint shape**

The existing `tests/test_api.py` uses an `app_with_data` fixture that
exposes `.get(url)`. Append this test:

```python
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
```

Run:

```bash
pytest tests/test_api.py::test_tool_error_rate_returns_expected_shape -v
```

Expected: PASS. (`buckets` will likely be empty under the test
fixtures since `tool_error.jsonl` etc. aren't part of `app_with_data`'s
mini R2 mirror — that's fine; the shape contract still holds.)

- [ ] **Step 5: Commit**

```bash
git add backend/api.py tests/test_api.py
git commit -m "api: add /api/tool-error-rate endpoint"
```

---

## Task 6: Frontend — `ToolErrorRatePanel`

**Files:**
- Modify: `src/dashboard-charts-extra.jsx` (add component before the existing `ToolUsagePanel`, export at bottom).

- [ ] **Step 1: Add the panel component**

In `src/dashboard-charts-extra.jsx`, find the line `function ToolUsagePanel(` (around line 1399). Insert the following component IMMEDIATELY BEFORE that line:

```jsx
// Tool error rate (per-model sub-panels mirroring ContextGrowthPanel
// layout). Each sub-panel shows EMA(α=0.15) lines for "Aggregate"
// (all tools in the model) plus per-tool series. Default ON:
// Aggregate + top-3 tools by n_total over the visible range.
// Numerator = n_error, denominator = n_total over settled calls
// (is_error IS NOT NULL); unmatched calls excluded by the API.
function ToolErrorRatePanel({ project, range, nonce }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [data, setData] = React.useState([]);
  const [bucketMs, setBucketMs] = React.useState(86_400_000);
  const [tip, setTip] = React.useState(null);

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  React.useEffect(() => {
    const q = (project ? `&project=${encodeURIComponent(project)}` : '');
    fetch(`/api/tool-error-rate?range=${range || 'all'}${q}`,
          { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => {
        setData(b.buckets || []);
        if (b.bucket_s) setBucketMs(b.bucket_s * 1000);
      })
      .catch(err => console.error('tool-error-rate fetch failed', err));
  }, [project, range, nonce]);

  // Group buckets by short model name. Each model gets:
  //   { buckets: sorted bucket timestamps (ms),
  //     perBucketTool: Map<ts, Map<tool, {n_total, n_error}>>,
  //     totalsByTool: Map<tool, n_total> }
  const byModel = React.useMemo(() => {
    const out = {};
    for (const r of data || []) {
      const t = Date.parse(r.ts);
      if (isNaN(t)) continue;
      const key = window.shortModelName ? window.shortModelName(r.model) : r.model;
      if (!key || key === '<synthetic>' || key === 'synthetic') continue;
      if (!out[key]) out[key] = {
        perBucketTool: new Map(),
        totalsByTool:  new Map(),
        bucketSet:     new Set(),
      };
      const M = out[key];
      M.bucketSet.add(t);
      if (!M.perBucketTool.has(t)) M.perBucketTool.set(t, new Map());
      const cur = M.perBucketTool.get(t).get(r.tool) || { n_total: 0, n_error: 0 };
      cur.n_total += r.n_total;
      cur.n_error += r.n_error;
      M.perBucketTool.get(t).set(r.tool, cur);
      M.totalsByTool.set(r.tool, (M.totalsByTool.get(r.tool) || 0) + r.n_total);
    }
    for (const k of Object.keys(out)) {
      out[k].buckets = [...out[k].bucketSet].sort((a, b) => a - b);
      delete out[k].bucketSet;
    }
    return out;
  }, [data]);

  const models = React.useMemo(() => {
    return Object.entries(byModel)
      .map(([m, v]) => {
        let total = 0;
        for (const n of v.totalsByTool.values()) total += n;
        return { model: m, total };
      })
      .sort((a, b) => b.total - a.total);
  }, [byModel]);

  const cellW = Math.max(280, (w - 16) / 2);
  const cellH = 230;

  // Pair sub-panels into rows of 2.
  const rows = [];
  for (let i = 0; i < models.length; i += 2) rows.push(models.slice(i, i + 2));

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}` }}>
        <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
          Tool Error Rate
        </div>
        <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
          per-model EMA (α=0.15) of n_error / n_total · only tool calls with a settled tool_result counted
        </div>
      </div>

      {!models.length && (
        <div style={{ padding: 16, color: TH_X.textDim, fontFamily: 'monospace', fontSize: 12 }}>
          no tool calls in range
        </div>
      )}

      {rows.map((row, ri) => (
        <div key={ri} style={{
          display: 'flex', gap: 8, padding: 8,
          borderTop: ri === 0 ? `1px solid ${TH_X.border}` : 'none',
        }}>
          {row.map(m => (
            <ToolErrorSubPanel key={m.model}
              modelName={m.model}
              modelData={byModel[m.model]}
              w={cellW} h={cellH}
              bucketMs={bucketMs}
              setTip={setTip} />
          ))}
          {row.length === 1 && <div style={{ width: cellW }} />}
        </div>
      ))}

      {tip && (
        <div style={{
          position: 'fixed', left: tip.x + 12, top: tip.y + 12,
          background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
          padding: '6px 8px', borderRadius: 3, pointerEvents: 'none',
          fontFamily: 'monospace', fontSize: 11, color: TH_X.text,
          whiteSpace: 'pre', zIndex: 1000,
        }}>
          {tip.text}
        </div>
      )}
    </div>
  );
}

function ToolErrorSubPanel({ modelName, modelData, w, h, bucketMs, setTip }) {
  const AGGREGATE = '__AGG__';

  // Top-3 tools by total count for this model.
  const topTools = React.useMemo(() => {
    return [...modelData.totalsByTool.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(e => e[0]);
  }, [modelData]);

  const allTools = React.useMemo(() =>
    [...modelData.totalsByTool.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(e => e[0])
  , [modelData]);

  // Default ON: AGGREGATE + topTools. User overrides layered on top.
  const [overrides, setOverrides] = React.useState({});
  const sel = React.useMemo(() => {
    const s = new Set([AGGREGATE, ...topTools]);
    for (const [k, on] of Object.entries(overrides)) {
      if (on) s.add(k); else s.delete(k);
    }
    return s;
  }, [topTools, overrides]);

  function toggle(k) {
    setOverrides(prev => ({ ...prev, [k]: !sel.has(k) }));
  }

  // Build per-series rate sequences. Each series: array of
  // { t_ms, rate } at non-sparse buckets only (n_total > 0).
  const series = React.useMemo(() => {
    const out = new Map();
    out.set(AGGREGATE, []);
    for (const tool of allTools) out.set(tool, []);
    for (const ts of modelData.buckets) {
      const m = modelData.perBucketTool.get(ts);
      // Aggregate
      let aT = 0, aE = 0;
      for (const v of m.values()) { aT += v.n_total; aE += v.n_error; }
      if (aT > 0) out.get(AGGREGATE).push({ t_ms: ts, rate: aE / aT, n_total: aT, n_error: aE });
      // Per tool
      for (const tool of allTools) {
        const v = m.get(tool);
        if (v && v.n_total > 0) {
          out.get(tool).push({ t_ms: ts, rate: v.n_error / v.n_total, n_total: v.n_total, n_error: v.n_error });
        }
      }
    }
    return out;
  }, [modelData, allTools]);

  // EMA over the rate sequence for each visible series.
  const emaSeries = React.useMemo(() => {
    const ALPHA = 0.15;
    const out = new Map();
    for (const k of sel) {
      const arr = series.get(k);
      if (!arr || !arr.length) { out.set(k, []); continue; }
      const ema = [];
      let prev = arr[0].rate;
      ema.push({ ...arr[0], ema: prev });
      for (let i = 1; i < arr.length; i++) {
        prev = ALPHA * arr[i].rate + (1 - ALPHA) * prev;
        ema.push({ ...arr[i], ema: prev });
      }
      out.set(k, ema);
    }
    return out;
  }, [series, sel]);

  // Y axis: 0 → max EMA across visible series, +10% headroom.
  const yMax = React.useMemo(() => {
    let m = 0;
    for (const k of sel) {
      const arr = emaSeries.get(k) || [];
      for (const p of arr) if (p.ema > m) m = p.ema;
    }
    return Math.max(m * 1.1, 0.001);  // never let max collapse to 0
  }, [emaSeries, sel]);

  // X axis: bucket range across the model.
  const xMin = modelData.buckets.length ? modelData.buckets[0] : 0;
  const xMax = modelData.buckets.length ? modelData.buckets[modelData.buckets.length - 1] + bucketMs : 1;

  const padL = 38, padR = 6, padT = 22, padB = 22;
  const plotW = Math.max(1, w - padL - padR);
  const plotH = Math.max(1, h - padT - padB);
  const xs = (t) => padL + ((t - xMin) / Math.max(1, xMax - xMin)) * plotW;
  const ys = (v) => padT + plotH - (v / yMax) * plotH;

  function colorFor(key) {
    if (key === AGGREGATE) return '#ddd';
    const palette = ['#ee4444', '#44dd66', '#dd66aa', '#44bbbb', '#eeaa44', '#9966dd', '#bb88ff', '#66ddee'];
    const idx = allTools.indexOf(key);
    return palette[idx >= 0 ? idx % palette.length : 0];
  }

  function labelFor(key) {
    return key === AGGREGATE ? 'Aggregate' : key;
  }

  return (
    <div style={{ width: w, border: `1px solid ${TH_X.border}`, borderRadius: 3 }}>
      <div style={{ padding: '6px 10px', fontFamily: 'monospace', fontSize: 11,
                    color: TH_X.text, fontWeight: 700, borderBottom: `1px solid ${TH_X.border}` }}>
        {modelName}
      </div>

      {/* Checkbox row */}
      <div style={{
        padding: '4px 10px', display: 'flex', flexWrap: 'wrap', gap: '4px 10px',
        fontFamily: 'monospace', fontSize: 10, color: TH_X.textDim,
        borderBottom: `1px solid ${TH_X.border}`,
      }}>
        {[AGGREGATE, ...allTools].map(k => {
          const c = colorFor(k);
          const checked = sel.has(k);
          return (
            <label key={k} style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              cursor: 'pointer', userSelect: 'none', opacity: checked ? 1 : 0.55,
            }}>
              <input type="checkbox" checked={checked} onChange={() => toggle(k)}
                style={{ accentColor: c, margin: 0 }} />
              <span style={{ width: 8, height: 8, background: c, display: 'inline-block', borderRadius: 2 }} />
              <span style={{ color: TH_X.text }}>{labelFor(k)}</span>
            </label>
          );
        })}
      </div>

      <svg width={w} height={h}>
        {/* y axis */}
        <line x1={padL} y1={padT} x2={padL} y2={padT + plotH} stroke={TH_X.border} />
        <line x1={padL} y1={padT + plotH} x2={padL + plotW} y2={padT + plotH} stroke={TH_X.border} />

        {/* y ticks: 0%, 50%, 100% of yMax */}
        {[0, 0.5, 1].map((f, i) => {
          const v = f * yMax;
          return (
            <g key={i}>
              <line x1={padL - 3} y1={ys(v)} x2={padL} y2={ys(v)} stroke={TH_X.border} />
              <text x={padL - 5} y={ys(v) + 3} textAnchor="end"
                    fontSize="9" fontFamily="monospace" fill={TH_X.textDim}>
                {(v * 100).toFixed(v < 0.01 ? 2 : 1)}%
              </text>
            </g>
          );
        })}

        {/* EMA polylines */}
        {[...sel].map(k => {
          const arr = emaSeries.get(k) || [];
          if (arr.length < 2) return null;
          const pts = arr.map(p => `${xs(p.t_ms + bucketMs / 2)},${ys(p.ema)}`).join(' ');
          return (
            <polyline key={k} points={pts} fill="none"
              stroke={colorFor(k)} strokeWidth={k === AGGREGATE ? 1.6 : 1.2} />
          );
        })}

        {/* hover capture: invisible rects per bucket */}
        {modelData.buckets.map((ts, i) => {
          const x0 = xs(ts);
          const x1 = xs(ts + bucketMs);
          return (
            <rect key={i} x={x0} y={padT} width={Math.max(1, x1 - x0)} height={plotH}
              fill="transparent"
              onMouseMove={(e) => {
                const m = modelData.perBucketTool.get(ts);
                let aT = 0, aE = 0;
                for (const v of m.values()) { aT += v.n_total; aE += v.n_error; }
                const lines = [
                  `${new Date(ts).toISOString().replace('T', ' ').slice(0, 16)} UTC`,
                  `aggregate: ${aE}/${aT} = ${aT ? ((aE / aT) * 100).toFixed(2) : '-'}%`,
                ];
                for (const tool of [...sel].filter(k => k !== AGGREGATE)) {
                  const v = m.get(tool);
                  if (v) lines.push(`${tool}: ${v.n_error}/${v.n_total} = ${v.n_total ? ((v.n_error / v.n_total) * 100).toFixed(2) : '-'}%`);
                }
                setTip({ x: e.clientX, y: e.clientY, text: lines.join('\n') });
              }}
              onMouseLeave={() => setTip(null)} />
          );
        })}
      </svg>
    </div>
  );
}
```

- [ ] **Step 2: Export at the bottom of the file**

Find the line `window.ToolUsagePanel = ToolUsagePanel;` (around line
2212) and add immediately after:

```jsx
window.ToolErrorRatePanel = ToolErrorRatePanel;
```

- [ ] **Step 3: Quick syntax check**

The file is loaded via in-browser Babel — there's no build step. Open
the dashboard in a browser and check the console for compile errors.
`TH_X` is already defined at the top of the file (line 4:
`const TH_X = window.dashboardTheme;`), so the new component picks it
up without further wiring.

- [ ] **Step 4: Commit**

```bash
git add src/dashboard-charts-extra.jsx
git commit -m "frontend: add ToolErrorRatePanel (per-model EMA panel)"
```

---

## Task 7: Wire the panel into the dashboard

**Files:**
- Modify: `src/app.jsx:719-727` (just after `<window.ReplyLatencyPanel ...>`)

- [ ] **Step 1: Mount the panel after Reply Latency**

In `src/app.jsx`, after the closing `</div>` of the
`<div className="dash-latency">` block (around line 727), add:

```jsx
      {backendOn && (
        <div className="dash-tool-errors">
          <window.ToolErrorRatePanel
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}
```

- [ ] **Step 2: Restart, reload, visually verify**

```bash
systemctl restart claudit
```

Open the dashboard in a browser. Scroll past Reply Latency. The new
panel should render with one sub-panel per model, default visible
series = Aggregate + top-3 tools, EMA polylines drawn. Hovering a
bucket shows the tooltip with `n_error/n_total` per visible series.

If the panel renders empty ("no tool calls in range"), trigger an
ingest:

```bash
curl -sS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/admin/ingest
```

Wait for `ingest_done` (visible in `journalctl -u claudit -f`),
reload the page (the SSE listener auto-refetches; no manual refresh
needed if the tab was open, but a reload is fine).

- [ ] **Step 3: Commit**

```bash
git add src/app.jsx
git commit -m "frontend: mount ToolErrorRatePanel after reply-latency"
```

---

## Final check — full test pass

- [ ] **Step 1: Run the full pytest suite**

```bash
pytest -v
```

Expected: all green. Existing tests untouched; three new tests pass
(error / success / unmatched).

- [ ] **Step 2: Visual regression check**

Open the dashboard and skim the existing panels (cost-by-model, token
breakdown, response sizes, tool usage, reply latency, context growth,
burn rate). Confirm none of them broke (the parser change extends
`tool_uses` rows but doesn't reshape any other output).
