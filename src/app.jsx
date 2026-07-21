// Main app shell: routes between Dashboard / Sessions list / Session detail.
// Loads synthetic events for the dashboard preview; lets you drop a real
// .jsonl on the Session view to inspect a single transcript.

const { useState, useEffect, useMemo, useRef } = React;

function txToDashData(tx) {
  // Convert a real transcript into dashboard-shaped {events, limitHits, range}.
  // Each event = ONE assistant turn (after applying the parse_session
  // turn-stats algorithm: user-text boundaries → last usage per turn).
  // Rates come from the shared window.modelRates table (parser.js) so the
  // Inspector, Token Breakdown, and this path can never disagree on pricing.
  const rateFor = window.rateForModel;
  const shortM = (model) => window.shortModelName(model || 'unknown');

  // Group all assistant_usage records by sessionId, plus user-text events
  // per session for turn boundaries (only available when loaded via Load N).
  const usageBySid = new Map();
  for (const u of tx.meta) {
    if (u.type !== 'assistant_usage') continue;
    const sid = u.sessionId || 'live';
    if (!usageBySid.has(sid)) usageBySid.set(sid, []);
    usageBySid.get(sid).push(u);
  }

  // Get user-text lines per session for turn boundaries.
  const boundaryLinesBySid = new Map();
  const allEv = tx.eventsBySession ? null : tx.events;
  if (tx.eventsBySession) {
    for (const [sid, evs] of tx.eventsBySession) {
      const lines = [];
      for (const e of evs) {
        if (e.type === 'user_message' && typeof e.detail === 'string' && e.detail.trim()) {
          lines.push(e.line);
        }
      }
      lines.sort((a, b) => a - b);
      boundaryLinesBySid.set(sid, lines);
    }
  } else if (allEv) {
    // Single-file path: all events live in tx.events; partition by sessionId
    // (which is on the usage records — we use the dominant one).
    const byS = new Map();
    for (const e of allEv) {
      if (e.type === 'user_message' && typeof e.detail === 'string' && e.detail.trim()) {
        const sid = e.sessionId || (usageBySid.size === 1 ? [...usageBySid.keys()][0] : 'live');
        if (!byS.has(sid)) byS.set(sid, []);
        byS.get(sid).push(e.line);
      }
    }
    for (const [sid, ls] of byS) { ls.sort((a,b)=>a-b); boundaryLinesBySid.set(sid, ls); }
  }

  const events = [];
  for (const [sid, usages] of usageBySid) {
    usages.sort((a, b) => a.line - b.line);
    const bounds = boundaryLinesBySid.get(sid) || [];
    // Bucket usages into turns: each bound starts a turn.
    // Usages before the first bound form turn 0 (initial system→assistant).
    const turns = [];
    let bi = 0;
    let cur = [];
    for (const u of usages) {
      while (bi < bounds.length && bounds[bi] <= u.line) {
        if (cur.length) turns.push(cur);
        cur = [];
        bi++;
      }
      cur.push(u);
    }
    if (cur.length) turns.push(cur);
    // If no bounds were found (single-file path with all usages, no user_text
    // captured), fall back to one-usage-per-turn so the panel still works.
    const turnUsages = bounds.length ? turns.map(t => t[t.length - 1]) : usages;
    let turnIdx = 0;
    for (const u of turnUsages) {
      const t = Date.parse(u.ts); if (isNaN(t)) continue;
      const us = u.usage || {};
      const inp = us.input_tokens || 0;
      const out = us.output_tokens || 0;
      const cc  = us.cache_creation_input_tokens || 0;
      const cr  = us.cache_read_input_tokens || 0;
      if ((inp + cc + cr) === 0) continue; // refusal/interrupt
      const eph5 = (us.cache_creation && us.cache_creation.ephemeral_5m_input_tokens) || 0;
      const eph1h = (us.cache_creation && us.cache_creation.ephemeral_1h_input_tokens) || 0;
      const r = rateFor(u.model);
      const unsplit = Math.max(0, cc - eph5 - eph1h);
      const cost = (inp * r.fresh + out * r.out + (eph5 + unsplit) * r.c5 + eph1h * r.c1h + cr * r.read) / 1_000_000;
      events.push({
        ts: t,
        session_id: sid,
        turn_index: turnIdx++,
        model: shortM(u.model),
        input_tokens: inp,
        output_tokens: out,
        cache_create: cc,
        cache_read: cr,
        ephemeral_5m: eph5,
        ephemeral_1h: eph1h,
        cost_usd: cost,
        ctx: window.usageCtxInput(us),
      });
    }
  }
  events.sort((a, b) => a.ts - b.ts);
  const limitHits = tx.meta
    .filter(m => m.type === 'rate_limit')
    .map(m => ({ ts: Date.parse(m.ts), text: m.content || 'rate limit' }))
    .filter(x => !isNaN(x.ts));
  if (!events.length) return null;
  const start = events[0].ts;
  const end = events[events.length - 1].ts;
  const pad = Math.max((end - start) * 0.02, 60_000);
  return { events, limitHits, range: { start: start - pad, end: end + pad } };
}

function App() {
  const [route, setRoute] = useState('dashboard'); // dashboard | sessions | session
  const [tx, setTx] = useState(null); // parsed transcript {events, meta, stats}
  const [, setFilename] = useState('');
  const [synth, setSynth] = useState(null);
  const [useSynth, setUseSynth] = useState(true);
  const [backendDash, setBackendDash] = useState(null);
  const [projects, setProjects] = useState(null);
  const [activeProject, setActiveProject] = useState('');
  const [activeRange, setActiveRange] = useState('all');
  const [models, setModels] = useState(null);
  // Server injects window.IS_GUEST into index.html so the very first
  // render already hides guest-restricted UI — no flash of the
  // Sessions/Inspector tabs before /api/me resolves.
  const [isGuest, setIsGuest] = useState(!!window.IS_GUEST);

  const backendOn = !!(window.BACKEND_URL && window.BACKEND_URL.length > 0);

  useEffect(() => { setSynth(window.generateSyntheticData()); }, []);

  // Identity probe — drives guest-mode UI gating.
  useEffect(() => {
    if (!backendOn) return;
    fetch('/api/me', { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setIsGuest(!!b.is_guest))
      .catch(() => {});
  }, [backendOn]);

  // Fetch project list once at boot if backend mode is on.
  // Skipped for guests — the endpoint is server-side blocked anyway,
  // and the picker isn't rendered in guest mode.
  useEffect(() => {
    if (!backendOn || isGuest) return;
    fetch('/api/projects', { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setProjects(b.projects || []))
      .catch(err => console.error('projects fetch failed', err));
  }, [backendOn, isGuest]);

  // Model list — distinct raw model strings + counts. Frontend dedups
  // by short name (e.g. claude-opus-4-7-* → opus-4-7).
  useEffect(() => {
    if (!backendOn) return;
    fetch('/api/models', { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setModels(b.models || []))
      .catch(err => console.error('models fetch failed', err));
  }, [backendOn]);

  // Fetch dashboard whenever the active project / range / nonce change.
  // `dashNonce` is a counter bumped by the SSE listener below to trigger
  // a re-fetch without changing project/range.
  const [dashNonce, setDashNonce] = useState(0);
  useEffect(() => {
    if (!backendOn) return;
    const q = activeProject ? `&project=${encodeURIComponent(activeProject)}` : '';
    fetch(`/api/dashboard?range=${activeRange}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => setBackendDash(b))
      .catch(err => console.error('dashboard fetch failed', err));
  }, [backendOn, activeProject, activeRange, dashNonce]);

  // Live updates: open an SSE stream and bump dashNonce on `ingest_done`.
  // No page reload — only the data refetches.
  useEffect(() => {
    if (!backendOn) return;
    const es = new EventSource('/api/events', { withCredentials: true });
    const onIngest = () => setDashNonce(n => n + 1);
    es.addEventListener('ingest_done', onIngest);
    es.onerror = () => { /* EventSource auto-reconnects with backoff */ };
    return () => { es.removeEventListener('ingest_done', onIngest); es.close(); };
  }, [backendOn]);

  const liveData = useMemo(() => tx ? txToDashData(tx) : null, [tx]);
  const dashData = backendDash
    ? backendDashToShape(backendDash)
    : ((!useSynth && liveData) ? liveData : synth);

  async function loadFromBackend(sessionId) {
    try {
      const r = await fetch(`/api/sessions/${sessionId}/transcript`, { credentials: 'same-origin' });
      const text = await r.text();
      const { events, meta } = window.parseTranscript(text);
      const stats = window.computeSessionStats(events, meta);
      setTx({ events, meta, stats });
      setFilename(sessionId);
      setUseSynth(false);
      setRoute('session');
    } catch (err) {
      console.error('transcript fetch failed', err);
    }
  }

  return (
    <div className="app-root">
      <TopBar route={route} setRoute={setRoute} isGuest={isGuest} backendOn={backendOn} range={activeRange} project={activeProject} />
      {backendOn && !isGuest && projects && (
        <ProjectPicker
          projects={projects}
          active={activeProject}
          onChange={setActiveProject}
        />
      )}
      {backendOn && (
        <RangePicker active={activeRange} onChange={setActiveRange} />
      )}
      {route === 'dashboard' && dashData && <Dashboard synth={dashData} models={models} backendOn={backendOn} activeProject={activeProject} activeRange={activeRange} dashNonce={dashNonce} />}
      {route === 'sessions' && dashData && (
        <SessionsList
          synth={dashData}
          onOpen={(sid) => backendOn ? loadFromBackend(sid) : setRoute('session')}
        />
      )}
      {route === 'cache' && backendOn && (
        <div>
          <window.CacheView project={activeProject} range={activeRange} />
          <window.ContextGrowthAgg project={activeProject} range={activeRange} />
        </div>
      )}
      {route === 'session' && <SessionView tx={tx} />}
    </div>
  );
}

function RangePicker({ active, onChange }) {
  const presets = [
    { label: '24h',  value: '1d'   },
    { label: '7d',   value: '7d'   },
    { label: '30d',  value: '30d'  },
    { label: '90d',  value: '90d'  },
    { label: '1y',   value: '365d' },
    { label: 'all',  value: 'all'  },
  ];
  return (
    <div className="project-picker" style={{ borderTop: '1px solid #2a2a4a' }}>
      <span style={{ color: '#9090b0', fontFamily: 'monospace', fontSize: 11, marginRight: 8 }}>range:</span>
      {presets.map(p => (
        <button
          key={p.value}
          className={'pp-btn ' + (active === p.value ? 'on' : '')}
          onClick={() => onChange(p.value)}
        >{p.label}</button>
      ))}
    </div>
  );
}

function ExportButton({ range, project }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const onClick = async () => {
    if (busy) return;
    setBusy(true);
    setErr('');
    const url = `/api/export?range=${encodeURIComponent(range)}` +
      (project ? `&project=${encodeURIComponent(project)}` : '');
    try {
      const res = await fetch(url, { credentials: 'same-origin' });
      if (!res.ok) {
        let msg = `export failed (${res.status})`;
        try { const j = await res.json(); if (j && j.detail) msg = j.detail; } catch (_) {}
        setErr(msg);
        return;
      }
      const blob = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objUrl;
      a.download = `ccusage_${project || 'all'}_${range}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(objUrl);
    } catch (e) {
      setErr('export failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center' }}>
      <button
        className="pp-btn"
        style={{ marginLeft: 12, opacity: busy ? 0.5 : 1 }}
        onClick={onClick}
        disabled={busy}
        title="Download a PNG of this dashboard for the current filters"
      >{busy ? 'rendering…' : 'Export PNG'}</button>
      {err && <span style={{ color: '#ff6b6b', fontSize: 11, marginLeft: 8 }}>{err}</span>}
    </span>
  );
}

// /api/projects returns every project ordered by total_cost DESC, which is
// enough chips to push the dashboard below the fold. Page the list; "All" and
// the pager stay pinned outside it so the reset control and the page controls
// are reachable from every page.
const PROJECTS_PER_PAGE = 24;

function ProjectPicker({ projects, active, onChange }) {
  const [page, setPage] = useState(0);

  const pageCount = Math.max(1, Math.ceil(projects.length / PROJECTS_PER_PAGE));
  // Clamp rather than store a corrected page: if `projects` shrinks under us
  // (refetch with fewer rows), a stale index would strand the user on a blank
  // page with no chips to click their way out of.
  const safePage = Math.min(page, pageCount - 1);
  const start = safePage * PROJECTS_PER_PAGE;
  const shown = projects.slice(start, start + PROJECTS_PER_PAGE);

  // The active chip may live on another page. Nothing renders as `on` then —
  // including "All" — so surface the selection instead of leaving the filtered
  // dashboard looking unfiltered.
  const activeOffPage = active !== '' && !shown.some(p => p.project_id === active);

  return (
    <div className="project-picker">
      <button className={'pp-btn ' + (active === '' ? 'on' : '')} onClick={() => onChange('')}>All</button>
      {shown.map(p => (
        <button
          key={p.project_id}
          className={'pp-btn ' + (active === p.project_id ? 'on' : '')}
          onClick={() => onChange(p.project_id)}
          title={`${p.session_count} sessions · $${p.total_cost.toFixed(2)}`}
        >{p.display_name}</button>
      ))}
      {pageCount > 1 && (
        <span className="pp-pager">
          <button
            className="pp-btn pp-nav"
            onClick={() => setPage(safePage - 1)}
            disabled={safePage === 0}
            title="Previous page"
          >‹</button>
          <span className="pp-count">{safePage + 1} / {pageCount}</span>
          <button
            className="pp-btn pp-nav"
            onClick={() => setPage(safePage + 1)}
            disabled={safePage >= pageCount - 1}
            title="Next page"
          >›</button>
          {activeOffPage && (
            <button
              className="pp-btn on pp-jump"
              onClick={() => setPage(Math.floor(
                projects.findIndex(p => p.project_id === active) / PROJECTS_PER_PAGE
              ))}
              title="Jump to the selected project"
            >{active} ↩</button>
          )}
        </span>
      )}
    </div>
  );
}

// Convert backend /api/dashboard response → the {events, limitHits, range}
// shape the existing Dashboard component expects. The hourly aggregates
// are mapped to one synthetic "event" per hour bucket so the dashboard
// panels render correctly. Per-turn detail is loaded separately via the
// Inspector when a user opens a session.
function backendDashToShape(b) {
  // Canonicalize raw backend model strings ("claude-opus-4-7-20251101")
  // to short names ("opus-4-7") ONCE here, so every downstream consumer
  // (model colors, Cost by Model labels, burn-rate dots) agrees.
  const short = m => window.shortModelName(m || 'unknown');
  const events = (b.hourly || []).map((h, i) => ({
    ts: Date.parse(h.hour),
    session_id: 'backend-h' + i,
    turn_index: 0,
    model: short(h.model),
    input_tokens: h.input_tokens,
    output_tokens: h.output_tokens,
    cache_create: h.cache_5m_tokens + h.cache_1h_tokens,
    cache_read: h.cache_read_tokens,
    ephemeral_5m: h.cache_5m_tokens,
    ephemeral_1h: h.cache_1h_tokens,
    cost_usd: h.cost_usd,
    requests: h.requests || 1,
    session_count: h.session_count || 0,
  })).filter(e => !isNaN(e.ts));
  if (!events.length) return null;
  const start = events[0].ts;
  // +1ms so the bin loop's strict `events[ci].ts < bEnd` includes the
  // last event (its ts == range.end otherwise gets dropped, dropping
  // ~1 bucket from the cumulative line and creating the $14.2K vs
  // $15K mismatch with the summary stat).
  const end = events[events.length - 1].ts + 1;
  const costByModel = (b.cost_by_model || []).reduce((acc, r) => {
    const key = short(r.model);
    acc[key] = (acc[key] || 0) + (r.cost_usd || 0);
    return acc;
  }, {});
  const limitHits = (b.rate_limit_hits || [])
    .map(h => ({ ts: Date.parse(h.ts), text: h.content || 'rate limit' }))
    .filter(h => !isNaN(h.ts));
  const sessions = (b.sessions || []).map(s => {
    const startMs = (s.start_ts || 0) * 1000;
    const endMs = (s.end_ts || s.start_ts || 0) * 1000;
    const synthEvent = {
      ts: startMs,
      session_id: s.session_id,
      turn_index: 0,
      model: short(s.model),
      input_tokens: s.input_tokens,
      output_tokens: s.output_tokens,
      cache_create: s.cache_create_tokens,
      cache_read: s.cache_read_tokens,
      ephemeral_5m: 0,
      ephemeral_1h: 0,
      cost_usd: s.cost_usd,
      requests: s.requests,
    };
    return {
      start: startMs,
      end: endMs,
      events: [synthEvent],
      ctxEnd: s.ctx_at_end != null ? s.ctx_at_end : null,
      session_id: s.session_id,
      requests: s.requests,
      model: short(s.model),
      models_used: (s.models_used || []).map(short),
      turns: s.turns || [],
    };
  });
  return {
    events, limitHits, range: { start, end }, costByModel,
    sessionsOverride: sessions,
    totalSessions: b.total_sessions,
    mainWUsage: b.main_w_usage,
    mainEmpty: b.main_empty,
    subagentFiles: b.subagent_files,
    subagentOnlySessions: b.subagent_only_sessions,
    totalPrompts: b.total_prompts,
    totalTurns: b.total_turns,
    responseSizes: b.response_sizes || [],
    ctxTraces: b.ctx_traces || [],
    bucketS: b.bucket_s || 86400,
  };
}

function TopBar({ route, setRoute, isGuest, backendOn, range, project }) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <div className="logo">
          <span className="logo-mark">{'>'}</span>
          <span className="logo-text">CLAUDIT</span>
          <span className="logo-sub">token usage audit{isGuest ? ' · guest' : ''}</span>
        </div>
      </div>
      <nav className="topnav">
        <button className={'navbtn ' + (route === 'dashboard' ? 'on' : '')} onClick={() => setRoute('dashboard')}>Overview</button>
        {!isGuest && (
          <button className={'navbtn ' + (route === 'sessions' ? 'on' : '')} onClick={() => setRoute('sessions')}>Sessions</button>
        )}
        {backendOn && (
          <button className={'navbtn ' + (route === 'cache' ? 'on' : '')} onClick={() => setRoute('cache')}>Cache</button>
        )}
        {!isGuest && (
          <button className={'navbtn ' + (route === 'session' ? 'on' : '')} onClick={() => setRoute('session')}>Inspector</button>
        )}
      </nav>
      <div className="topbar-right">
        {backendOn && !isGuest && (
          <ExportButton range={range} project={project} />
        )}
        <a className="loadbtn logout-btn" href="/logout">Logout</a>
      </div>
    </header>
  );
}

// ─────────────────────────────────────────────────────────────────
// Dashboard view
// ─────────────────────────────────────────────────────────────────

function computeSessions(events) {
  if (!events.length) return { sessions: [], windowBoundaries: [] };
  // 30-min gap = new session; 5-hour gap = window boundary
  const sorted = events.slice().sort((a, b) => a.ts - b.ts);
  const sessions = [];
  const windowBoundaries = [];
  let cur = { start: sorted[0].ts, end: sorted[0].ts, events: [sorted[0]] };
  for (let i = 1; i < sorted.length; i++) {
    const gap = sorted[i].ts - sorted[i-1].ts;
    if (gap > 30 * 60 * 1000) {
      cur.end = sorted[i-1].ts;
      sessions.push(cur);
      if (gap > 5 * 60 * 60 * 1000) windowBoundaries.push((sorted[i].ts + sorted[i-1].ts)/2);
      cur = { start: sorted[i].ts, end: sorted[i].ts, events: [sorted[i]] };
    } else {
      cur.events.push(sorted[i]);
      cur.end = sorted[i].ts;
    }
  }
  sessions.push(cur);
  return { sessions, windowBoundaries };
}

// Compute Token Breakdown rows (tokens + TTL-split cost per type) from a
// set of hourly events. Shared by TokenBreakdownPanel; the per-panel model
// filter passes a pre-filtered subset of events. Cost is always TTL-split
// (eph5 × c5, eph1h × c1h, unsplit cache_create charged at the 5m rate).
function computeTokenBreakdown(events) {
  const t = { input: 0, output: 0, cc: 0, cr: 0, eph5: 0, eph1h: 0 };
  for (const e of events) {
    t.input += e.input_tokens; t.output += e.output_tokens;
    t.cc += e.cache_create; t.cr += e.cache_read;
    t.eph5 += e.ephemeral_5m; t.eph1h += e.ephemeral_1h;
  }
  const tokenTotal = t.input + t.output + t.cc + t.cr;
  const ccUnsplit = Math.max(0, t.cc - t.eph5 - t.eph1h);

  const c = { input: 0, output: 0, eph5: 0, eph1h: 0, ccUnsplit: 0, cr: 0 };
  if (window.rateForModel) {
    for (const e of events) {
      const r = window.rateForModel(e.model, e.ts);
      const unsplit = Math.max(0, (e.cache_create || 0) - (e.ephemeral_5m || 0) - (e.ephemeral_1h || 0));
      c.input     += (e.input_tokens   || 0) * r.fresh;
      c.output    += (e.output_tokens  || 0) * r.out;
      c.eph5      += (e.ephemeral_5m   || 0) * r.c5;
      c.eph1h     += (e.ephemeral_1h   || 0) * r.c1h;
      c.ccUnsplit += unsplit                  * r.c5;  // unsplit at 5m rate
      c.cr        += (e.cache_read     || 0) * r.read;
    }
    for (const k of Object.keys(c)) c[k] = c[k] / 1_000_000;
  }
  const costTotal = c.input + c.output + c.eph5 + c.eph1h + c.ccUnsplit + c.cr;

  const rows = [
    { label: 'Input',             value: t.input,  cost: c.input,     color: window.dashboardCol.inputTokens },
    { label: 'Output',            value: t.output, cost: c.output,    color: window.dashboardCol.outputTokens },
    { label: 'Cache Create (5m)', value: t.eph5,   cost: c.eph5,      color: window.dashboardCol.cacheCreateTokens },
    { label: 'Cache Create (1h)', value: t.eph1h,  cost: c.eph1h,     color: '#d488ff' },
    ...(ccUnsplit > 0
      ? [{ label: 'Cache Create (unsplit)', value: ccUnsplit, cost: c.ccUnsplit, color: '#7733aa' }]
      : []),
    { label: 'Cache Read',        value: t.cr,     cost: c.cr,        color: window.dashboardCol.cacheReadTokens },
  ].filter(r => r.value > 0).sort((a, b) => b.cost - a.cost);

  return { rows, tokenTotal: tokenTotal || 1, costTotal: costTotal || 1 };
}

// Paired token/cost breakdown bars with a per-panel model filter, mirroring
// the model select on Tool Usage Ratio over Time. The filter is client-side
// (events are already loaded) and applies to both bars at once.
function TokenBreakdownPanel({ events }) {
  const [activeModel, setActiveModel] = useState('');

  // Model options derived from the events actually present (already short
  // names via backendDashToShape), ordered by cost desc.
  const modelOpts = useMemo(() => {
    const byModel = {};
    for (const e of events) {
      if (!e.model || e.model === '<synthetic>' || e.model === 'synthetic') continue;
      byModel[e.model] = (byModel[e.model] || 0) + (e.cost_usd || 0);
    }
    return Object.entries(byModel).sort((a, b) => b[1] - a[1]).map(([k]) => k);
  }, [events]);

  const filtered = useMemo(
    () => (activeModel ? events.filter(e => e.model === activeModel) : events),
    [events, activeModel]);
  const { rows, tokenTotal, costTotal } = useMemo(
    () => computeTokenBreakdown(filtered), [filtered]);

  // One bordered card (matching the sibling "Cost by Model" card) so the
  // shared model filter visibly belongs to the whole Token Breakdown panel
  // — the two bars are the same data sorted by tokens vs by cost, so a
  // single picker drives both.
  return (
    <div style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 4, display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'flex-end',
        gap: 8, padding: '8px 14px 0',
        fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)',
      }}>
        <span>model:</span>
        <select
          value={activeModel}
          onChange={e => setActiveModel(e.target.value)}
          style={{
            background: 'var(--panel-2)', color: 'var(--fg)',
            border: '1px solid var(--border)', borderRadius: 4,
            padding: '3px 6px', fontFamily: 'var(--mono)', fontSize: 11,
            cursor: 'pointer',
          }}>
          <option value="">All</option>
          {modelOpts.map(m => <option key={m} value={m}>{m}</option>)}
        </select>
      </div>
      <window.HBar
        embedded
        title="Token Breakdown — by tokens"
        rows={[...rows].sort((a, b) => b.value - a.value)}
        fmt={r => `${window.humanFmt(r.value)} (${(r.value / tokenTotal * 100).toFixed(1)}%)`} />
      <window.HBar
        embedded
        title="Token Breakdown — by cost"
        rows={[...rows].map(r => ({ ...r, value: r.cost })).sort((a, b) => b.value - a.value)}
        fmt={r => `${window.humanCurrency(r.value)} (${(r.value / costTotal * 100).toFixed(1)}%)`} />
    </div>
  );
}

function Dashboard({ synth, models, backendOn, activeProject, activeRange, dashNonce }) {
  const { events, limitHits, range, costByModel: backendByModel, sessionsOverride, totalSessions, mainWUsage, mainEmpty, subagentFiles, subagentOnlySessions, totalPrompts, totalTurns, responseSizes, ctxTraces, bucketS } = synth;
  const hasBackendByModel = backendByModel && Object.keys(backendByModel).length > 0;
  const computed = useMemo(() => computeSessions(events), [events]);
  const sessions = (sessionsOverride && sessionsOverride.length)
    ? sessionsOverride
    : computed.sessions;
  const windowBoundaries = computed.windowBoundaries;

  const totals = useMemo(() => {
    const t = { input: 0, output: 0, cc: 0, cr: 0, cost: 0, eph5: 0, eph1h: 0 };
    const byModel = {};
    for (const e of events) {
      t.input += e.input_tokens; t.output += e.output_tokens;
      t.cc += e.cache_create; t.cr += e.cache_read;
      t.cost += e.cost_usd;
      t.eph5 += e.ephemeral_5m; t.eph1h += e.ephemeral_1h;
      byModel[e.model] = (byModel[e.model] || 0) + e.cost_usd;
    }
    t.total = t.input + t.output + t.cc + t.cr;
    return { ...t, byModel: hasBackendByModel ? backendByModel : byModel };
  }, [events, backendByModel, hasBackendByModel]);

  // Auto-pick bin size: at least 100 buckets, never coarser than 1 day.
  // Pick the LARGEST nice-bin in [60s, 1d] that still produces ≥100 bins
  // across the visible range; if even 60s overshoots, that's fine.
  const span = range.end - range.start;
  const MIN_BINS = 100;
  const MAX_BIN_MS = 24 * 3600 * 1000; // 1 day
  const niceBins = [60_000, 5*60_000, 15*60_000, 30*60_000, 3600_000, 6*3600_000, 12*3600_000, 24*3600_000];
  let binMs = niceBins[0];
  for (const b of niceBins) {
    if (b > MAX_BIN_MS) break;
    if (span / b < MIN_BINS) break;
    binMs = b;
  }

  const costByModel = Object.entries(totals.byModel)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value }));

  const totalCostStr = window.humanFmt(totals.cost, true);

  return (
    <div className="dashboard">
      <div className="dash-summary">
        <Stat label="window" value={`${window.fmtDate(range.start, {day:true})} – ${window.fmtDate(range.end, {day:true})}`} />
        <Stat label="main sessions with usage" value={(mainWUsage != null ? mainWUsage : (totalSessions != null ? totalSessions : (events.reduce((s, e) => s + (e.session_count || 0), 0) || sessions.length))).toLocaleString()} />
        {mainEmpty != null && <Stat label="main empty sessions" value={mainEmpty.toLocaleString()} />}
        {subagentFiles != null && <Stat label="subagent sessions" value={subagentFiles.toLocaleString()} />}
        {subagentOnlySessions != null && <Stat label="subagent-only sessions" value={subagentOnlySessions.toLocaleString()} />}
        {(mainWUsage != null || mainEmpty != null || subagentFiles != null) &&
          <Stat label="total" value={((mainWUsage || 0) + (mainEmpty || 0) + (subagentFiles || 0)).toLocaleString()} />}
        {totalPrompts != null && <Stat label="prompts" value={totalPrompts.toLocaleString()} />}
        {totalTurns != null && <Stat label="turns" value={totalTurns.toLocaleString()} />}
        <Stat label="requests" value={events.reduce((s, e) => s + (e.requests == null ? 1 : e.requests), 0).toLocaleString()} />
        <Stat label="total tokens" value={window.humanFmt(totals.total)} />
        <Stat label="total cost" value={totalCostStr} highlight />
        <Stat label="rate-limit hits" value={String(limitHits.length)} warn={limitHits.length > 0} />
      </div>

      <div className="dash-grid">
        <window.TimeSeriesPanel title="Input Tokens"  events={events} valueKey="input_tokens"
          color={window.dashboardCol.inputTokens} range={range} binMs={binMs} />
        <window.TimeSeriesPanel title="Output Tokens" events={events} valueKey="output_tokens"
          color={window.dashboardCol.outputTokens} range={range} binMs={binMs} />
        <window.TimeSeriesPanel title="Cache Create"  events={events} valueKey="cache_create"
          color={window.dashboardCol.cacheCreateTokens} range={range} binMs={binMs} />
        <window.TimeSeriesPanel title="Cache Read"    events={events} valueKey="cache_read"
          color={window.dashboardCol.cacheReadTokens} range={range} binMs={binMs} />
        <window.TimeSeriesPanel title="Total Tokens"  events={events.map(e => ({...e, _t: e.input_tokens+e.output_tokens+e.cache_create+e.cache_read}))}
          valueKey="_t" color={window.dashboardCol.totalTokens} range={range} binMs={binMs} />
        <window.TimeSeriesPanel title="Cost (USD)"    events={events} valueKey="cost_usd"
          color={window.dashboardCol.costUSD} range={range} binMs={binMs} isCurrency />
      </div>

      <div className="dash-grid-2">
        <window.HBar
          title="Cost by Model"
          rows={costByModel}
          fixedColors={window.modelColors}
          fmt={r => window.humanCurrency(r.value)} />
        <TokenBreakdownPanel events={events} />
      </div>

      <div className="dash-ttl">
        <window.CacheTTLPanel events={events} range={range} binMs={binMs} />
      </div>

      {responseSizes && responseSizes.length > 0 && (
        <div className="dash-resp">
          <window.ResponseSizesPanel data={responseSizes} bucketS={bucketS} />
        </div>
      )}

      {backendOn && (
        <div className="dash-tools">
          <window.ToolUsagePanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      {backendOn && (
        <div className="dash-latency">
          <window.ReplyLatencyPanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      {backendOn && (
        <div className="dash-tool-errors">
          <window.ToolErrorRatePanel
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      {backendOn && (
        <div className="dash-heatmap">
          <window.ActivityHeatmapPanel
            models={models}
            project={activeProject}
            range={activeRange}
            nonce={dashNonce} />
        </div>
      )}

      <div className="dash-context">
        <window.ContextGrowthPanel events={events} realSessions={sessionsOverride} ctxTraces={ctxTraces} />
      </div>

      <div className="dash-burn">
        <window.BurnRatePanel
          events={events}
          sessions={sessions}
          limitHits={limitHits}
          range={range}
          windowBoundaries={windowBoundaries} />
      </div>

    </div>
  );
}


function Stat({ label, value, highlight, warn }) {
  return (
    <div className={'stat ' + (highlight ? 'stat-hl ' : '') + (warn ? 'stat-warn ' : '')}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Sessions list
// ─────────────────────────────────────────────────────────────────

function SessionsList({ synth, onOpen }) {
  const [sort, setSort] = useState('recent');

  const rows = useMemo(() => {
    let arr;
    // Backend mode: use real per-session rows (with REAL session_ids).
    // The `sessionsOverride` array carries cost/tokens already summed
    // across main + sub-agent files via the deduped CTE, so cost-sort
    // matches the user's mental model.
    if (synth.sessionsOverride && synth.sessionsOverride.length) {
      arr = synth.sessionsOverride.map(s => {
        const ev = (s.events && s.events[0]) || {};
        const total = (ev.input_tokens || 0) + (ev.output_tokens || 0)
                    + (ev.cache_create || 0) + (ev.cache_read || 0);
        return {
          id: s.session_id,
          start: s.start, end: s.end,
          durMin: (s.end - s.start) / 60000,
          reqs: s.requests != null ? s.requests : (ev.requests || 0),
          cost: ev.cost_usd || 0,
          total,
          primary: window.shortModelName ? window.shortModelName(ev.model) : (ev.model || 'unknown'),
        };
      });
    } else {
      // Synth/live fallback: cluster the hourly events as before.
      const { sessions } = computeSessions(synth.events);
      arr = sessions.map((s, i) => {
        const sums = { input: 0, output: 0, cc: 0, cr: 0, cost: 0 };
        const models = {};
        for (const e of s.events) {
          sums.input += e.input_tokens; sums.output += e.output_tokens;
          sums.cc += e.cache_create; sums.cr += e.cache_read;
          sums.cost += e.cost_usd;
          models[e.model] = (models[e.model] || 0) + 1;
        }
        let primary = 'opus-4-6', max = 0;
        for (const [m, c] of Object.entries(models)) if (c > max) { max = c; primary = m; }
        return {
          id: 'S' + String(i + 1).padStart(4, '0'),
          start: s.start, end: s.end,
          durMin: (s.end - s.start) / 60000,
          reqs: s.events.length,
          cost: sums.cost,
          total: sums.input + sums.output + sums.cc + sums.cr,
          primary,
        };
      });
    }
    if (sort === 'recent') arr.sort((a, b) => b.start - a.start);
    else if (sort === 'cost') arr.sort((a, b) => b.cost - a.cost);
    else if (sort === 'tokens') arr.sort((a, b) => b.total - a.total);
    return arr;
  }, [synth, sort]);

  return (
    <div className="sessions-page">
      <div className="page-head">
        <h2>Sessions</h2>
        <div className="sort-row">
          <span className="muted">sort:</span>
          {['recent', 'cost', 'tokens'].map(k =>
            <button key={k} className={'chip ' + (sort === k ? 'on' : '')} onClick={() => setSort(k)}>{k}</button>
          )}
          <span className="muted right">showing {rows.length} sessions</span>
        </div>
      </div>
      <div className="sessions-table">
        <div className="srow shead">
          <div>id</div><div>started</div><div>duration</div><div>model</div>
          <div className="num">requests</div><div className="num">tokens</div><div className="num">cost</div><div></div>
        </div>
        {rows.slice(0, 80).map(r => (
          <div key={r.id} className="srow">
            <div className="mono" title={r.id} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {r.id.length > 10 ? r.id.slice(0, 8) + '…' : r.id}
            </div>
            <div>{window.fmtDate(r.start, { full: true })}</div>
            <div className="mono">{r.durMin < 60 ? r.durMin.toFixed(0)+'m' : (r.durMin/60).toFixed(1)+'h'}</div>
            <div>
              <span className="model-dot" style={{ background: window.modelColors[r.primary] || '#888' }}></span>
              <span className="mono">{r.primary}</span>
            </div>
            <div className="num mono">{r.reqs}</div>
            <div className="num mono">{window.humanFmt(r.total)}</div>
            <div className="num mono">{window.humanCurrency(r.cost)}</div>
            <div className="num"><button className="open-btn" onClick={() => onOpen(r.id)}>open ›</button></div>
          </div>
        ))}
      </div>
      <div className="page-foot muted">List of {rows.length} sessions reconstructed from <code>usage_events</code> via 30-minute gap rule. Click <em>open</em> to drop a real .jsonl into the inspector.</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────
// Session view
// ─────────────────────────────────────────────────────────────────

function SessionView({ tx }) {
  const [selected, setSelected] = useState(0);
  const [filter, setFilter] = useState({ user: true, asst: true, think: true, tool: true, result: true });
  const [search, setSearch] = useState('');
  const [dense, setDense] = useState(false);
  const [view, setView] = useState('timeline'); // timeline | ctx


  if (!tx) {
    return (
      <div className="session-empty">
        <div className="drop-card">
          <div className="drop-title">No session selected</div>
          <div className="drop-sub">Pick a session from the Sessions list to inspect its
            timeline, tool calls, and context growth.</div>
        </div>
      </div>
    );
  }

  const visible = tx.events.filter(e => {
    if (e.type === 'user_message' && !filter.user) return false;
    if (e.type === 'assistant_text' && !filter.asst) return false;
    if (e.type === 'thinking' && !filter.think) return false;
    if ((e.type === 'tool_call' || e.type === 'agent_spawn') && !filter.tool) return false;
    if (e.type === 'tool_result' && !filter.result) return false;
    if (search) {
      const hay = (e.detail || '') + ' ' + (e.tool_name || '') + ' ' + JSON.stringify(e.tool_input || '');
      if (!hay.toLowerCase().includes(search.toLowerCase())) return false;
    }
    return true;
  });

  const sel = visible[selected] || null;

  return (
    <div className="session-view">
      <SessionHeader stats={tx.stats} />
      <div style={{
        display: 'flex', gap: 6, alignItems: 'center',
        padding: '8px 14px', borderBottom: '1px solid var(--border)',
        background: 'var(--bg-soft)',
      }}>
        {[['timeline', 'Timeline'], ['ctx', 'Context growth']].map(([k, lab]) => (
          <button key={k} className={'fchip ' + (view === k ? 'on' : '')}
            onClick={() => setView(k)}>{lab}</button>
        ))}
      </div>
      {view === 'ctx' && <window.ContextGrowthView tx={tx} />}
      {view === 'timeline' && (
      <div className="session-body">
        <aside className="session-side">
          <div className="filterbar">
            <input className="search" placeholder="search transcript…" value={search} onChange={e => setSearch(e.target.value)} />
            <div className="filter-chips">
              {[
                ['user','User'],['asst','Asst'],['think','Think'],['tool','Tools'],['result','Results']
              ].map(([k, lab]) => (
                <button key={k} className={'fchip ' + (filter[k] ? 'on' : '')}
                  onClick={() => setFilter(f => ({ ...f, [k]: !f[k] }))}>{lab}</button>
              ))}
              <span className="spacer"></span>
              <button className={'fchip ' + (dense ? 'on' : '')} onClick={() => setDense(d => !d)}>dense</button>
            </div>
          </div>
          <div className="timeline">
            {visible.map((e, idx) => (
              <TimelineRow key={e.line + ':' + idx} e={e} dense={dense}
                selected={idx === selected} onClick={() => setSelected(idx)} />
            ))}
          </div>
        </aside>
        <main className="session-detail">
          <window.EventDetail event={sel} dense={dense} />
        </main>
      </div>
      )}
    </div>
  );
}

function SessionHeader({ stats }) {
  const dur = stats.lastTs && stats.firstTs ? (stats.lastTs - stats.firstTs)/60000 : 0;
  return (
    <div className="session-header">
      <Stat label="turns"      value={stats.turns} />
      <Stat label="user msgs"  value={stats.userMsgs} />
      <Stat label="tool calls" value={stats.toolCalls} />
      <Stat label="errors"     value={stats.errorResults} warn={stats.errorResults > 0} />
      <Stat label="parallel batches" value={stats.parallelBatches} />
      <Stat label="duration"   value={dur < 60 ? dur.toFixed(0)+'m' : (dur/60).toFixed(1)+'h'} />
      <Stat label="output tokens" value={window.humanFmt(stats.output)} />
      <Stat label="cache hit %" value={stats.hitRate.toFixed(1) + '%'} />
      <Stat label="est. cost"  value={window.humanCurrency(stats.cost)} highlight />
    </div>
  );
}

function TimelineRow({ e, dense, selected, onClick }) {
  const meta = window.TYPE_META[e.type] || { label: e.type, color: 'var(--fg)', glyph: '·' };
  const oneLine = window.eventOneLine(e).slice(0, 220);
  const toolColor = e.tool_name ? window.TOOL_COLORS[e.tool_name] : null;
  return (
    <div className={'trow ' + (selected ? 'sel ' : '') + (dense ? 'dense ' : '') + (e.is_error ? 'err ' : '')}
      onClick={onClick}>
      <div className="trow-time mono">{window.shortTime(e.ts)}</div>
      <div className="trow-tag mono" style={{ color: meta.color, borderColor: meta.color + '40' }}>
        {meta.label}
      </div>
      <div className="trow-glyph mono" style={{ color: toolColor || meta.color }}>
        {e.tool_name ? window.toolGlyph(e.tool_name) : meta.glyph}
      </div>
      <div className="trow-body">
        <span className="trow-one">{oneLine}</span>
        {e.batch_size > 1 && <span className="trow-batch">⫶{e.batch_index}/{e.batch_size}</span>}
        {e.is_error && <span className="trow-err">ERR</span>}
      </div>
    </div>
  );
}

window.App = App;
