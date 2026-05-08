// Main app shell: routes between Dashboard / Sessions list / Session detail.
// Loads synthetic events for the dashboard preview; lets you drop a real
// .jsonl on the Session view to inspect a single transcript.

const { useState, useEffect, useMemo, useRef, useCallback } = React;

const SAMPLE_KEY = 'cc_viz_synthetic';

function txToDashData(tx) {
  // Convert a real transcript into dashboard-shaped {events, limitHits, range}.
  // Each event = ONE assistant turn (after applying the parse_session
  // turn-stats algorithm: user-text boundaries → last usage per turn).
  const RATES = {
    'opus':   { in: 15/1e6, out: 75/1e6, c5: 18.75/1e6, c1h: 30/1e6, read: 1.5/1e6 },
    'sonnet': { in: 3/1e6,  out: 15/1e6, c5: 3.75/1e6,  c1h: 6/1e6,  read: 0.3/1e6 },
    'haiku':  { in: 1/1e6,  out: 5/1e6,  c5: 1.25/1e6,  c1h: 2/1e6,  read: 0.1/1e6 },
  };
  function rateFor(model) {
    const m = (model || '').toLowerCase();
    if (m.includes('opus')) return RATES.opus;
    if (m.includes('haiku')) return RATES.haiku;
    return RATES.sonnet;
  }
  function shortM(model) {
    let s = model || 'unknown';
    const mm = s.match(/(opus|sonnet|haiku)[-_]?(\d[-_]?\d?)/i);
    return mm ? `${mm[1].toLowerCase()}-${mm[2].replace('_','-')}` : s;
  }

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
      const cost = inp * r.in + out * r.out + (eph5 + unsplit) * r.c5 + eph1h * r.c1h + cr * r.read;
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
  const [filename, setFilename] = useState('');
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
  const dataLabel = backendDash
    ? `backend: ${activeProject || 'all projects'}`
    : ((!useSynth && liveData) ? `live: ${filename}` : 'synthetic preview');

  function loadFile(file) {
    const reader = new FileReader();
    reader.onload = e => {
      const text = String(e.target.result || '');
      const { events, meta } = window.parseTranscript(text);
      const stats = window.computeSessionStats(events, meta);
      setTx({ events, meta, stats });
      setFilename(file.name);
      setUseSynth(false);
      setRoute('session');
    };
    reader.readAsText(file);
  }

  // Load N .jsonl files at once: union all assistant_usage and rate_limit
  // metas into a single tx-shaped object so the dashboard sees real
  // multi-session data. The Inspector still works on the FIRST file's
  // events, but Overview / Sessions get the merged view.
  // Accepts a mix of .jsonl/.json/.txt and .zip — zips are unpacked first.
  async function loadFiles(filesArg) {
    let arr = Array.from(filesArg || []);
    if (!arr.length) return;
    // Expand zips into virtual File-like objects
    const expanded = [];
    for (const f of arr) {
      const isZip = f.name.toLowerCase().endsWith('.zip') ||
                    f.type === 'application/zip' || f.type === 'application/x-zip-compressed';
      if (isZip && window.JSZip) {
        try {
          const zip = await window.JSZip.loadAsync(f);
          const entries = Object.values(zip.files).filter(e =>
            !e.dir && /\.(jsonl|json|txt)$/i.test(e.name));
          for (const e of entries) {
            const blob = await e.async('blob');
            expanded.push(new File([blob], e.name.split('/').pop(), { type: 'text/plain' }));
          }
        } catch (err) {
          console.error('Failed to unzip', f.name, err);
        }
      } else {
        expanded.push(f);
      }
    }
    arr = expanded;
    if (!arr.length) return;
    // Sequential parse with shared cross-file uuid dedup, mirroring
    // parse_session.py's directory mode. The same API call recorded into
    // two files (main + agent-*.jsonl) is processed once.
    const seenUuids = new Set();
    const all = { events: [], meta: [] };
    // Per-session event arrays so we can compute true turn boundaries
    // (user-text → next user-text) for each session independently.
    const evBySession = new Map();
    let firstParsed = null;
    let dupedRecs = 0;
    const readText = (file) => new Promise(resolve => {
      const r = new FileReader();
      r.onload = e => resolve(String(e.target.result || ''));
      r.readAsText(file);
    });
    for (let idx = 0; idx < arr.length; idx++) {
      const file = arr[idx];
      const text = await readText(file);
      const before = seenUuids.size;
      const { events, meta } = window.parseTranscript(text, { seenUuids });
      const after = seenUuids.size;
      // Rough estimate: lines that ran through parse minus uniques added.
      // Not exact, but useful for the status pill.
      dupedRecs += Math.max(0, (text.split('\n').length - 1) - (after - before)) - meta.length;
      if (idx === 0) firstParsed = { events, meta, name: file.name };
      const fallbackSid = file.name.replace(/\.jsonl$/, '');
      // Resolve a sessionId for this file: prefer sessionId on usage records,
      // fall back to filename. One file ≈ one logical session in CC.
      let sid = fallbackSid;
      for (const m of meta) {
        if (m.type === 'assistant_usage' && m.sessionId) { sid = m.sessionId; break; }
      }
      for (const m of meta) {
        if (m.type === 'assistant_usage' && !m.sessionId) m.sessionId = sid;
        all.meta.push(m);
      }
      // Stash this file's events under its session for turn analysis.
      // We tag each event with its sessionId so cross-file merges (multiple
      // files sharing a sessionId) are concatenated correctly.
      const evWithSid = events.map(e => ({ ...e, sessionId: sid }));
      if (!evBySession.has(sid)) evBySession.set(sid, []);
      evBySession.get(sid).push(...evWithSid);
      all.events.push(...evWithSid);
    }
    const stats = window.computeSessionStats(firstParsed.events, all.meta);
    setTx({
      events: firstParsed.events,
      meta: all.meta,
      stats,
      eventsBySession: evBySession,
    });
    setFilename(`${arr.length} files merged · ${seenUuids.size.toLocaleString()} uniq recs`);
    setUseSynth(false);
    setRoute('dashboard');
  }

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
      <TopBar route={route} setRoute={setRoute} isGuest={isGuest} />
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
      {route === 'dashboard' && dashData && <Dashboard synth={dashData} dataLabel={dataLabel} models={models} backendOn={backendOn} activeProject={activeProject} activeRange={activeRange} dashNonce={dashNonce} />}
      {route === 'sessions' && dashData && (
        <SessionsList
          synth={dashData}
          onOpen={(sid) => backendOn ? loadFromBackend(sid) : setRoute('session')}
        />
      )}
      {route === 'session' && <SessionView tx={tx} loadFile={loadFile} />}
    </div>
  );
}

function ModelPicker({ models, active, onChange }) {
  // Dedup by short name (claude-opus-4-7-20251101 → opus-4-7), sum
  // counts. Lets a single "opus-4-7" choice cover both date-suffixed
  // and bare model strings via the backend's LIKE %model% filter.
  const grouped = {};
  for (const m of models || []) {
    const key = window.shortModelName ? window.shortModelName(m.model) : m.model;
    if (key === '<synthetic>' || key === 'synthetic') continue;
    grouped[key] = (grouped[key] || 0) + (m.n || 0);
  }
  const opts = Object.entries(grouped)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => ({ key: k, n }));
  return (
    <div className="project-picker" style={{ borderTop: '1px solid #2a2a4a' }}>
      <span style={{ color: '#9090b0', fontFamily: 'monospace', fontSize: 11, marginRight: 8 }}>model:</span>
      <select
        value={active}
        onChange={e => onChange(e.target.value)}
        style={{
          background: '#16172e', color: '#e6e8f0',
          border: '1px solid #2a2c44', borderRadius: 4,
          padding: '4px 8px', fontFamily: 'monospace', fontSize: 12,
          cursor: 'pointer',
        }}
      >
        <option value="">All ({Object.values(grouped).reduce((s, n) => s + n, 0).toLocaleString()})</option>
        {opts.map(o => (
          <option key={o.key} value={o.key}>
            {o.key} ({o.n.toLocaleString()})
          </option>
        ))}
      </select>
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

function ProjectPicker({ projects, active, onChange }) {
  return (
    <div className="project-picker">
      <button className={'pp-btn ' + (active === '' ? 'on' : '')} onClick={() => onChange('')}>All</button>
      {projects.map(p => (
        <button
          key={p.project_id}
          className={'pp-btn ' + (active === p.project_id ? 'on' : '')}
          onClick={() => onChange(p.project_id)}
          title={`${p.session_count} sessions · $${p.total_cost.toFixed(2)}`}
        >{p.display_name}</button>
      ))}
    </div>
  );
}

// Convert backend /api/dashboard response → the {events, limitHits, range}
// shape the existing Dashboard component expects. The hourly aggregates
// are mapped to one synthetic "event" per hour bucket so the dashboard
// panels render correctly. Per-turn detail is loaded separately via the
// Inspector when a user opens a session.
function backendDashToShape(b) {
  const events = (b.hourly || []).map((h, i) => ({
    ts: Date.parse(h.hour),
    session_id: 'backend-h' + i,
    turn_index: 0,
    model: h.model || 'unknown',
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
    acc[r.model] = (acc[r.model] || 0) + (r.cost_usd || 0);
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
      model: s.model || 'unknown',
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
      model: s.model || 'unknown',
      models_used: s.models_used || [],
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
    responseSizes: b.response_sizes || [],
    ctxTraces: b.ctx_traces || [],
    bucketS: b.bucket_s || 86400,
  };
}

function TopBar({ route, setRoute, isGuest }) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <div className="logo">
          <span className="logo-mark">▮▮</span>
          <span className="logo-text">CCUSAGE</span>
          <span className="logo-sub">/ session inspector{isGuest ? ' · guest' : ''}</span>
        </div>
      </div>
      <nav className="topnav">
        <button className={'navbtn ' + (route === 'dashboard' ? 'on' : '')} onClick={() => setRoute('dashboard')}>Overview</button>
        {!isGuest && (
          <button className={'navbtn ' + (route === 'sessions' ? 'on' : '')} onClick={() => setRoute('sessions')}>Sessions</button>
        )}
        {!isGuest && (
          <button className={'navbtn ' + (route === 'session' ? 'on' : '')} onClick={() => setRoute('session')}>Inspector</button>
        )}
      </nav>
      <div className="topbar-right">
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

function Dashboard({ synth, dataLabel, models, backendOn, activeProject, activeRange, dashNonce }) {
  const { events, limitHits, range, costByModel: backendByModel, sessionsOverride, totalSessions, mainWUsage, mainEmpty, subagentFiles, subagentOnlySessions, responseSizes, ctxTraces, bucketS } = synth;
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
  function binLabel(ms) {
    if (ms < 3600_000) return (ms/60_000) + 'm';
    if (ms < 24*3600_000) return (ms/3600_000) + 'h';
    return (ms/(24*3600_000)) + 'd';
  }

  // Cache Create is split into 5m and 1h ephemerals; any leftover
  // (legacy SDK rows that didn't carry the ephemeral_* split) is
  // bucketed under "Cache Create (unsplit)" so it stays visible.
  const ccUnsplit = Math.max(0, totals.cc - totals.eph5 - totals.eph1h);

  // Per-token-type cost — sum tokens × per-model rate over hourly
  // events using the shared rate table from parser.js. Lets the Token
  // Breakdown panel show "{tokens} ({tok%}), ${cost} ({cost%})" per row.
  const costByType = useMemo(() => {
    const c = { input: 0, output: 0, eph5: 0, eph1h: 0, ccUnsplit: 0, cr: 0, total: 0 };
    if (!window.rateForModel) return c;
    for (const e of events) {
      const r = window.rateForModel(e.model);
      const unsplit = Math.max(0, (e.cache_create || 0) - (e.ephemeral_5m || 0) - (e.ephemeral_1h || 0));
      c.input     += (e.input_tokens   || 0) * r.fresh;
      c.output    += (e.output_tokens  || 0) * r.out;
      c.eph5      += (e.ephemeral_5m   || 0) * r.c5;
      c.eph1h     += (e.ephemeral_1h   || 0) * r.c1h;
      c.ccUnsplit += unsplit                  * r.c5;  // unsplit at 5m rate
      c.cr        += (e.cache_read     || 0) * r.read;
    }
    for (const k of Object.keys(c)) c[k] = c[k] / 1_000_000;
    c.total = c.input + c.output + c.eph5 + c.eph1h + c.ccUnsplit + c.cr;
    return c;
  }, [events]);

  const tokenBreakdown = [
    { label: 'Input',             value: totals.input,  cost: costByType.input,     color: window.dashboardCol.inputTokens },
    { label: 'Output',            value: totals.output, cost: costByType.output,    color: window.dashboardCol.outputTokens },
    { label: 'Cache Create (5m)', value: totals.eph5,   cost: costByType.eph5,      color: window.dashboardCol.cacheCreateTokens },
    { label: 'Cache Create (1h)', value: totals.eph1h,  cost: costByType.eph1h,     color: '#d488ff' },
    ...(ccUnsplit > 0
      ? [{ label: 'Cache Create (unsplit)', value: ccUnsplit, cost: costByType.ccUnsplit, color: '#7733aa' }]
      : []),
    { label: 'Cache Read',        value: totals.cr,     cost: costByType.cr,        color: window.dashboardCol.cacheReadTokens },
  ].filter(r => r.value > 0).sort((a, b) => b.cost - a.cost);
  const tokenBreakdownTotal = totals.total || 1;
  const tokenBreakdownCostTotal = costByType.total || 1;

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
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <window.HBar
            title="Token Breakdown — by tokens"
            rows={[...tokenBreakdown].sort((a, b) => b.value - a.value)}
            fmt={r => `${window.humanFmt(r.value)} (${(r.value / tokenBreakdownTotal * 100).toFixed(1)}%)`} />
          <window.HBar
            title="Token Breakdown — by cost"
            rows={[...tokenBreakdown]
              .map(r => ({ ...r, value: r.cost }))
              .sort((a, b) => b.value - a.value)}
            fmt={r => `${window.humanCurrency(r.value)} (${(r.value / tokenBreakdownCostTotal * 100).toFixed(1)}%)`} />
        </div>
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

// Small-multiples token breakdown — replaces an HBar where Cache Read
// (typically 98%+ of tokens) made every other row visually invisible.
// Each cell: color stripe, type label, big tokens value with %, cost
// with %. Sorted by cost (the question the reader actually has).
function TokenBreakdownPanel({ rows, tokenTotal, costTotal }) {
  return (
    <div style={{
      background: '#16213e',
      border: '1px solid #2a2a4a',
      borderRadius: 4,
      padding: '14px 16px 16px',
    }}>
      <div style={{
        textAlign: 'center', color: '#e0e0e0',
        fontFamily: 'monospace', fontSize: 13, fontWeight: 700,
        marginBottom: 12,
      }}>Token Breakdown</div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: 10,
      }}>
        {rows.map((r) => {
          const tokPct  = (r.value / tokenTotal * 100);
          const costPct = (r.cost  / costTotal  * 100);
          return (
            <div key={r.label} style={{
              background: '#0f1428',
              borderLeft: `3px solid ${r.color}`,
              borderRadius: 2,
              padding: '8px 12px',
            }}>
              <div style={{
                color: r.color, fontFamily: 'monospace',
                fontSize: 10.5, fontWeight: 700,
                letterSpacing: 0.5, marginBottom: 6,
                whiteSpace: 'nowrap', overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}>{r.label}</div>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'baseline', gap: 8,
              }}>
                <span style={{
                  fontFamily: 'monospace', fontSize: 16,
                  fontWeight: 600, color: '#e0e0e0',
                }}>{window.humanFmt(r.value)}</span>
                <span style={{
                  fontFamily: 'monospace', fontSize: 10,
                  color: '#7e84a3',
                }}>{tokPct.toFixed(1)}%</span>
              </div>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'baseline', gap: 8, marginTop: 2,
              }}>
                <span style={{
                  fontFamily: 'monospace', fontSize: 13,
                  fontWeight: 600, color: '#ffdd00',
                }}>{window.humanCurrency(r.cost)}</span>
                <span style={{
                  fontFamily: 'monospace', fontSize: 10,
                  color: '#7e84a3',
                }}>{costPct.toFixed(1)}%</span>
              </div>
            </div>
          );
        })}
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

function SessionView({ tx, loadFile }) {
  const [selected, setSelected] = useState(0);
  const [filter, setFilter] = useState({ user: true, asst: true, think: true, tool: true, result: true });
  const [search, setSearch] = useState('');
  const [dense, setDense] = useState(false);
  const [showThinking, setShowThinking] = useState(true);
  const dropRef = useRef(null);

  useEffect(() => {
    const el = dropRef.current; if (!el) return;
    const over = e => { e.preventDefault(); el.classList.add('drag'); };
    const leave = () => el.classList.remove('drag');
    const drop = e => {
      e.preventDefault(); el.classList.remove('drag');
      if (e.dataTransfer.files[0]) loadFile(e.dataTransfer.files[0]);
    };
    el.addEventListener('dragover', over);
    el.addEventListener('dragleave', leave);
    el.addEventListener('drop', drop);
    return () => {
      el.removeEventListener('dragover', over);
      el.removeEventListener('dragleave', leave);
      el.removeEventListener('drop', drop);
    };
  }, [loadFile]);

  if (!tx) {
    return (
      <div className="session-empty" ref={dropRef}>
        <div className="drop-card">
          <div className="drop-glyph">⬇</div>
          <div className="drop-title">Drop a .jsonl transcript here</div>
          <div className="drop-sub">Or use the <em>Load .jsonl</em> button up top. Files are parsed in your browser — nothing leaves the page.</div>
          <div className="drop-hints">
            <span>~/.claude/projects/&lt;hash&gt;/&lt;session-uuid&gt;.jsonl</span>
          </div>
        </div>
      </div>
    );
  }

  const visible = tx.events.filter(e => {
    if (e.type === 'user_message' && !filter.user) return false;
    if (e.type === 'assistant_text' && !filter.asst) return false;
    if (e.type === 'thinking' && (!filter.think || !showThinking)) return false;
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
