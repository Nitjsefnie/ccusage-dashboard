# Activity Heatmap Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New dashboard panel: 7×24 activity heatmap (weekdays × hours) in Czech local time (`Europe/Prague`), DST-aware via Postgres tzdata.

**Architecture:** One new read endpoint `GET /api/activity-heatmap` in `backend/api.py` (weekday×hour grouping in SQL with `AT TIME ZONE`, read-time uuid dedup), plus one self-fetching React panel `ActivityHeatmapPanel` in `src/dashboard-charts-extra.jsx`, mounted in `Dashboard` behind the `backendOn` gate — same contract as `ToolUsagePanel`.

**Tech Stack:** FastAPI + psycopg3 (raw SQL), React 18 via in-browser Babel (no build step), pytest.

**Spec:** `docs/superpowers/specs/2026-07-13-activity-heatmap-design.md` (committed; read it if a decision seems ambiguous).

## Global Constraints

- Parameterised SQL only (`%s` placeholders); never interpolate user input or the timezone string into query text.
- Cross-file uuid dedup at READ time via `DISTINCT ON (uuid)` (SV-PARSER-SPEC); no new tables, no schema change, no `PARSER_VERSION` bump.
- JS style: React function components + hooks, `window.`-attached globals, `camelCase`; Python: `from __future__ import annotations` already present in `api.py`, type hints, no ORM.
- Test fixtures stay small (SV-FIXTURE-SIZE): the DST test inserts 2 rows directly into the test DB, no new fixture files.
- Every commit carries the implementer's co-author trailer (kimi → `Co-Authored-By: Kimi K2.6 <noreply@kimi.com>`).
- Pre-existing Pyright noise about psycopg `execute(str)` is expected — do not "fix" it.

---

### Task 1: Backend endpoint `GET /api/activity-heatmap` (TDD)

**Files:**
- Modify: `backend/api.py` (add endpoint after `tool_error_rate`, i.e. after line ~255; add `HEATMAP_TZ` constant near the other module constants below `router = APIRouter(prefix="/api")`)
- Test: `tests/test_api.py` (append tests at the end)

**Interfaces:**
- Consumes: existing helpers in `backend/api.py` — `_parse_range(s) -> timedelta` (raises HTTPException 400), `db.viz_conn()`, `@cache_response` from `backend.cache`.
- Produces: `GET /api/activity-heatmap?range=&project=&model=` returning
  `{"range": str, "tz": "Europe/Prague", "cells": [{"dow": int 1..7, "hour": int 0..23, "requests": int, "output_tokens": int, "cost_usd": float}]}` (sparse — only non-empty cells). Task 2's panel fetches exactly this shape.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/session-viz && python3 -m pytest tests/test_api.py -k activity_heatmap -v`
Expected: all 5 FAIL with 404 (`assert r.status_code == 200` → 404), because the route doesn't exist yet. (`test_activity_heatmap_bad_range_400` also fails: 404 ≠ 400.)

- [ ] **Step 3: Implement the endpoint**

In `backend/api.py`, add the constant right under `_export_lock = asyncio.Semaphore(1)`:

```python
# Activity-heatmap timezone. Bound as a SQL parameter (never interpolated);
# Postgres tzdata makes AT TIME ZONE fully DST-aware (CET/CEST transitions).
HEATMAP_TZ = "Europe/Prague"
```

Add the endpoint after `tool_error_rate` (keep `reply_latency` and everything else untouched):

```python
@router.get("/activity-heatmap")
@cache_response
async def activity_heatmap(
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Weekday × hour activity grid in HEATMAP_TZ local wall-clock time.

    dow is ISO (1=Mon … 7=Sun), hour 0–23. DST handled by Postgres
    tzdata via AT TIME ZONE — UTC+1 in winter (CET), UTC+2 in summer
    (CEST). Cross-file uuid dedup at read time, mirroring /api/dashboard
    (SV-PARSER-SPEC). Unlike dashboard's dedup_body, the model filter is
    applied to BOTH arms so uuid-less legacy rows also honour it."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta

    proj_filter = "AND f.project_id = %s" if project else ""
    model_filter = "AND r.model LIKE %s" if model else ""
    filt_args: list[Any] = [since]
    if project:
        filt_args.append(project)
    if model:
        filt_args.append(f"%{model}%")

    dedup_body = f"""
      (SELECT DISTINCT ON (r.uuid) r.ts, r.output_tokens, r.cost_usd
       FROM records r
       JOIN files f ON f.file_key = r.file_key
       WHERE r.ts >= %s {proj_filter} {model_filter} AND r.uuid IS NOT NULL
       ORDER BY r.uuid, r.file_key)
      UNION ALL
      (SELECT r.ts, r.output_tokens, r.cost_usd
       FROM records r
       JOIN files f ON f.file_key = r.file_key
       WHERE r.ts >= %s {proj_filter} {model_filter} AND r.uuid IS NULL)
    """
    # Placeholder order in the final SQL string: the two AT TIME ZONE
    # params sit in the SELECT list (before FROM), then the dedup body's
    # two filter arms.
    args = [HEATMAP_TZ, HEATMAP_TZ] + filt_args + filt_args

    with db.viz_conn() as c:
        rows = c.execute(
            f"""
            SELECT EXTRACT(ISODOW FROM (d.ts AT TIME ZONE %s))::int AS dow,
                   EXTRACT(HOUR   FROM (d.ts AT TIME ZONE %s))::int AS hour,
                   COUNT(*)             AS requests,
                   SUM(d.output_tokens) AS output_tokens,
                   SUM(d.cost_usd)      AS cost_usd
            FROM ({dedup_body}) d
            WHERE d.ts IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            args,
        ).fetchall()

    return {
        "range": range,
        "tz": HEATMAP_TZ,
        "cells": [
            {
                "dow": int(dow),
                "hour": int(hour),
                "requests": int(n or 0),
                "output_tokens": int(out or 0),
                "cost_usd": float(cost or 0),
            }
            for (dow, hour, n, out, cost) in rows
        ],
    }
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `cd /root/session-viz && python3 -m pytest tests/test_api.py -k activity_heatmap -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `cd /root/session-viz && python3 -m pytest tests/ -q`
Expected: all tests pass (same pass count as before + 5).

- [ ] **Step 6: Commit**

```bash
cd /root/session-viz
git add backend/api.py tests/test_api.py
git commit -m "feat(api): /api/activity-heatmap — weekday×hour grid in Europe/Prague (DST-aware)

Co-Authored-By: Kimi K2.6 <noreply@kimi.com>"
```

---

### Task 2: Frontend `ActivityHeatmapPanel` + mount + docs

**Files:**
- Modify: `src/dashboard-charts-extra.jsx` (add `ActivityHeatmapPanel` after the `ToolUsagePanel` function ends — after its closing `}` near the `window.` export block at the file bottom; register `window.ActivityHeatmapPanel` in that export block)
- Modify: `src/app.jsx` (mount in `Dashboard` after the `.dash-tool-errors` block, ~line 804)
- Modify: `README.md` (Features list), `AGENTS.md` (panel list in "Project overview")

**Interfaces:**
- Consumes: `GET /api/activity-heatmap?range=&project=&model=` from Task 1 (shape above); `window.DashTooltip` (props `{tip: {x, y, title, accent, lines: [[k, v], …]}}`), `window.humanFmt(n)`, `window.humanCurrency(n)`, `window.shortModelName(m)`, theme constants `TH_X` / `COL_X` already imported at the top of `dashboard-charts-extra.jsx`.
- Produces: `window.ActivityHeatmapPanel` with props `{models, project, range, nonce}` — the exact prop contract of `window.ToolUsagePanel`.

- [ ] **Step 1: Add the panel component**

In `src/dashboard-charts-extra.jsx`, after `ToolUsagePanel`'s closing brace, add:

```jsx
// ---------------------------------------------------------------------------
// Activity Heatmap — weekday × hour grid in Europe/Prague local time.
// DST handling lives in the backend (Postgres AT TIME ZONE); this panel
// only renders the dow/hour cells it is given.
const _HEAT_DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _HEAT_METRICS = [
  { key: 'requests',      label: 'requests',   color: 'oklch(0.78 0.14 245)',
    fmt: v => v.toLocaleString() },
  { key: 'output_tokens', label: 'output tok', color: COL_X.outputTokens,
    fmt: v => humanFmt_X(v) },
  { key: 'cost_usd',      label: 'cost',       color: COL_X.costUSD,
    fmt: v => window.humanCurrency(v) },
];

function ActivityHeatmapPanel({ models, project, range, nonce }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  const [cells, setCells] = React.useState([]);
  const [activeModel, setActiveModel] = React.useState('');
  const [metric, setMetric] = React.useState('requests');

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  React.useEffect(() => {
    const q = (project ? `&project=${encodeURIComponent(project)}` : '')
            + (activeModel ? `&model=${encodeURIComponent(activeModel)}` : '');
    fetch(`/api/activity-heatmap?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setCells(b.cells || []))
      .catch(err => console.error('activity-heatmap fetch failed', err));
  }, [project, range, activeModel, nonce]);

  // Dedup model list by short name for the select (same as ToolUsagePanel).
  const modelOpts = React.useMemo(() => {
    const grouped = {};
    for (const m of models || []) {
      const key = window.shortModelName ? window.shortModelName(m.model) : m.model;
      if (key === '<synthetic>' || key === 'synthetic') continue;
      grouped[key] = (grouped[key] || 0) + (m.n || 0);
    }
    return Object.entries(grouped)
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => ({ key: k, n }));
  }, [models]);

  const mspec = _HEAT_METRICS.find(m => m.key === metric) || _HEAT_METRICS[0];

  const { byCell, maxVal } = React.useMemo(() => {
    const byCell = new Map();               // dow*100+hour -> cell
    let maxVal = 0;
    for (const c of cells || []) {
      byCell.set(c.dow * 100 + c.hour, c);
      maxVal = Math.max(maxVal, c[metric] || 0);
    }
    return { byCell, maxVal };
  }, [cells, metric]);

  // Geometry — 24 columns × 7 rows, label gutters left + top.
  const padL = 44, padR = 14, padT = 24, padB = 10, gap = 2;
  const cellW = Math.max(8, (w - padL - padR - 23 * gap) / 24);
  const cellH = Math.min(34, Math.max(18, cellW * 0.8));
  const h = padT + 7 * cellH + 6 * gap + padB;

  // Sequential single-hue ramp on the dark surface: intensity = opacity
  // of the metric hue; sqrt keeps the heavy-tailed mid-range readable.
  function fillFor(v) {
    if (!v || maxVal <= 0) return TH_X.bgDark;
    const t = Math.sqrt(v / maxVal);
    return { color: mspec.color, opacity: Math.max(0.08, t) };
  }

  function cellRect(dow, hour) {
    return {
      x: padL + hour * (cellW + gap),
      y: padT + (dow - 1) * (cellH + gap),
    };
  }

  function onCellEnter(e, dow, hour) {
    const rect = ref.current.getBoundingClientRect();
    const c = byCell.get(dow * 100 + hour);
    setTip({
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      title: `${_HEAT_DOW[dow - 1]} ${String(hour).padStart(2, '0')}:00–${String((hour + 1) % 24).padStart(2, '0')}:00`,
      accent: mspec.color,
      lines: c ? [
        ['requests',   c.requests.toLocaleString()],
        ['output tok', humanFmt_X(c.output_tokens)],
        ['cost',       window.humanCurrency(c.cost_usd)],
      ] : [['activity', 'none']],
    });
  }

  const legendW = 120;

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, display: 'flex', alignItems: 'center', gap: 16 }}>
        <div style={{ flex: 1 }}>
          <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
            Activity Heatmap
          </div>
          <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
            {mspec.label} by weekday × hour · Europe/Prague (CET/CEST, DST-aware)
          </div>
        </div>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim }}>
          {_HEAT_METRICS.map(m => (
            <button key={m.key} type="button" onClick={() => setMetric(m.key)}
              style={{
                background: 'transparent',
                color: metric === m.key ? TH_X.text : TH_X.textDim,
                border: `1px solid ${metric === m.key ? m.color : TH_X.border}`,
                borderRadius: 3, padding: '2px 8px',
                fontFamily: 'monospace', fontSize: 11, cursor: 'pointer',
              }}
            >{m.label}</button>
          ))}
          <span style={{ marginLeft: 8 }}>model:</span>
          <select
            value={activeModel}
            onChange={e => setActiveModel(e.target.value)}
            style={{
              background: '#16172e', color: TH_X.text,
              border: `1px solid ${TH_X.border}`, borderRadius: 4,
              padding: '3px 6px', fontFamily: 'monospace', fontSize: 11,
              cursor: 'pointer',
            }}
          >
            <option value="">All</option>
            {modelOpts.map(o => (
              <option key={o.key} value={o.key}>{o.key}</option>
            ))}
          </select>
        </div>
      </div>

      <svg width="100%" height={h} style={{ display: 'block' }}
           onMouseLeave={() => setTip(null)}>
        {/* hour labels every 3h */}
        {[0, 3, 6, 9, 12, 15, 18, 21].map(hr => (
          <text key={hr} x={cellRect(1, hr).x + cellW / 2} y={padT - 8}
                textAnchor="middle" fill={TH_X.textDim}
                fontFamily="monospace" fontSize="9">{hr}</text>
        ))}
        {/* weekday labels */}
        {_HEAT_DOW.map((d, i) => (
          <text key={d} x={padL - 8} y={padT + i * (cellH + gap) + cellH / 2 + 3}
                textAnchor="end" fill={TH_X.textDim}
                fontFamily="monospace" fontSize="9">{d}</text>
        ))}
        {/* cells */}
        {Array.from({ length: 7 }, (_, di) => di + 1).map(dow =>
          Array.from({ length: 24 }, (_, hour) => {
            const { x, y } = cellRect(dow, hour);
            const c = byCell.get(dow * 100 + hour);
            const v = c ? (c[metric] || 0) : 0;
            const f = fillFor(v);
            return (
              <rect key={`${dow}-${hour}`} x={x} y={y}
                    width={cellW} height={cellH} rx="2"
                    fill={typeof f === 'string' ? f : f.color}
                    fillOpacity={typeof f === 'string' ? 1 : f.opacity}
                    stroke={v > 0 ? 'none' : TH_X.border}
                    strokeWidth={v > 0 ? 0 : 0.5}
                    onMouseMove={e => onCellEnter(e, dow, hour)} />
            );
          })
        )}
      </svg>

      <div style={{
        padding: '4px 14px 10px', display: 'flex', alignItems: 'center', gap: 8,
        fontFamily: 'monospace', fontSize: 10, color: TH_X.textDim,
      }}>
        <span>0</span>
        <svg width={legendW} height="10">
          <defs>
            <linearGradient id="heatLegendGrad" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%"  stopColor={mspec.color} stopOpacity="0.05" />
              <stop offset="100%" stopColor={mspec.color} stopOpacity="1" />
            </linearGradient>
          </defs>
          <rect x="0" y="0" width={legendW} height="10" rx="2" fill="url(#heatLegendGrad)" />
        </svg>
        <span>{maxVal > 0 ? mspec.fmt(maxVal) : 'no data'}</span>
        <span style={{ marginLeft: 'auto' }}>intensity ∝ √(value / max)</span>
      </div>

      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}
```

Then register it in the `window.` export block at the bottom of the file, next to `window.ToolUsagePanel = ToolUsagePanel;`:

```jsx
window.ActivityHeatmapPanel = ActivityHeatmapPanel;
```

- [ ] **Step 2: Mount in Dashboard**

In `src/app.jsx`, directly after the `.dash-tool-errors` block (the `{backendOn && (<div className="dash-tool-errors">…</div>)}` that closes around line 804), add:

```jsx
      {backendOn && (
        <div className="dash-heatmap">
          <window.ActivityHeatmapPanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}
```

(No CSS change needed — sibling wrappers like `.dash-tools` have no dedicated rules in `public/app.css`; the panel styles itself inline.)

- [ ] **Step 3: Document the panel**

`README.md` — in the Features list, after the **Reply Latency over Time** bullet, add:

```markdown
- **Activity Heatmap** — weekday × hour grid of request activity in
  Czech local time (`Europe/Prague`, DST-aware via Postgres
  `AT TIME ZONE`), with requests / output-tokens / cost metric toggle
  and a per-panel model filter.
```

`AGENTS.md` — in "Project overview", extend the panel enumeration sentence to include `Activity Heatmap` (append it before `Reply Latency` or at the end of the list — keep it one sentence).

- [ ] **Step 4: Sanity checks**

- Run: `cd /root/session-viz && python3 -m pytest tests/ -q` — Expected: all pass (frontend change can't break pytest; this guards accidental backend edits).
- Run: `grep -c "ActivityHeatmapPanel" src/dashboard-charts-extra.jsx src/app.jsx` — Expected: `src/dashboard-charts-extra.jsx:2+` (definition + window export) and `src/app.jsx:1`.
- Eyeball the JSX you added for balanced braces/parens (in-browser Babel has no compile step here; the reviewer gate + live check after deploy cover runtime).

- [ ] **Step 5: Commit**

```bash
cd /root/session-viz
git add src/dashboard-charts-extra.jsx src/app.jsx README.md AGENTS.md
git commit -m "feat(ui): Activity Heatmap panel — weekday×hour, Europe/Prague, metric toggle

Co-Authored-By: Kimi K2.6 <noreply@kimi.com>"
```
