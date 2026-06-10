// Per-turn context growth view for the Inspector.
// Mirrors `analyze --turn-stats` from parse_session.py:
//   1. Walk events chronologically.
//   2. User text messages with non-empty text content are turn boundaries.
//      (User messages that are tool_result-only don't count.)
//   3. For each turn, take the LAST assistant_usage record before the
//      next boundary as that turn's context size = input + cache_create + cache_read.
//   4. Drop turns where input == 0 (API refusals / interrupts).
//
// Renders: a sparkline-style chart of input over turns, plus a dense table.

function computeTurnStats(tx) {
  const allLines = [];
  // Build a single line-ordered list of {kind, line, ts, ...} events.
  // We need user text boundaries AND assistant_usage records, sorted by line.
  for (const e of tx.events) {
    if (e.type === 'user_message' && typeof e.detail === 'string' && e.detail.trim()) {
      // Skip likely tool_result-passthrough wrappers — those come through
      // pushUserContent as type:tool_result, not user_message, so a
      // user_message here always represents real user text.
      allLines.push({ kind: 'user_text', line: e.line, ts: e.ts });
    }
  }
  for (const m of tx.meta) {
    if (m.type === 'assistant_usage') {
      allLines.push({
        kind: 'usage',
        line: m.line,
        ts: m.ts,
        usage: m.usage,
        model: m.model,
      });
    }
  }
  allLines.sort((a, b) => a.line - b.line);

  // Walk: each user_text starts a new turn; collect usages until next user_text.
  const turns = [];
  let cur = null;
  for (const item of allLines) {
    if (item.kind === 'user_text') {
      if (cur && cur.usages.length) turns.push(cur);
      cur = { startLine: item.line, startTs: item.ts, usages: [] };
    } else if (item.kind === 'usage' && cur) {
      cur.usages.push(item);
    }
  }
  if (cur && cur.usages.length) turns.push(cur);

  // Reduce each turn to its LAST usage; compute context = input + cc + cr.
  const rows = [];
  let prevInput = null;
  for (let i = 0; i < turns.length; i++) {
    const t = turns[i];
    const last = t.usages[t.usages.length - 1];
    const u = last.usage || {};
    const inp = u.input_tokens || 0;
    const cc = u.cache_creation_input_tokens || 0;
    const cr = u.cache_read_input_tokens || 0;
    const out = u.output_tokens || 0;
    // Peak per-call context — max-of-iteration-totals when the turn
    // fanned out via advisor()/sub-agent; otherwise the top-level sum.
    // Top-level inp/cc/cr stay as billing sums for the breakdown columns.
    const ctx = window.usageCtxInput(u);
    if (ctx === 0) continue; // refusal / interrupt
    const delta = prevInput !== null ? ctx - prevInput : null;
    rows.push({
      turnNum: rows.length + 1,
      line: last.line,
      ts: last.ts,
      model: last.model,
      input: inp,
      cacheCreate: cc,
      cacheRead: cr,
      output: out,
      ctx,
      delta,
    });
    prevInput = ctx;
  }
  return rows;
}

function ContextGrowthView({ tx }) {
  const rows = React.useMemo(() => computeTurnStats(tx), [tx]);
  const [hoverIdx, setHoverIdx] = React.useState(null);

  if (!rows.length) {
    return (
      <div style={{ padding: 40, color: 'var(--muted)', fontFamily: 'var(--mono)' }}>
        No turns with non-zero input tokens were found in this transcript.
      </div>
    );
  }

  // Determine model cap (use dominant)
  const counts = {};
  for (const r of rows) counts[r.model] = (counts[r.model] || 0) + 1;
  let dom = '', max = 0;
  for (const [m, c] of Object.entries(counts)) if (c > max) { max = c; dom = m; }
  const cap = window.capForModel(shortModel(dom));

  const peakCtx = Math.max(...rows.map(r => r.ctx));
  const peakOut = Math.max(...rows.map(r => r.output));
  const totalInput = rows.reduce((s, r) => s + r.input, 0);
  const totalOutput = rows.reduce((s, r) => s + r.output, 0);
  const avgCtx = rows.reduce((s, r) => s + r.ctx, 0) / rows.length;

  return (
    <div className="ctx-growth">
      <div className="ctx-summary">
        <SummaryStat label="turns" value={rows.length} />
        <SummaryStat label="model" value={shortModel(dom)} />
        <SummaryStat label="cap" value={window.humanFmt(cap)} />
        <SummaryStat label="peak ctx" value={window.humanFmt(peakCtx) + ` (${(peakCtx/cap*100).toFixed(0)}%)`} />
        <SummaryStat label="avg ctx" value={window.humanFmt(Math.round(avgCtx))} />
        <SummaryStat label="total input" value={window.humanFmt(totalInput)} />
        <SummaryStat label="total output" value={window.humanFmt(totalOutput)} />
      </div>

      <ContextChart rows={rows} cap={cap} peakOut={peakOut}
        hoverIdx={hoverIdx} setHoverIdx={setHoverIdx} />

      <div className="ctx-table-wrap">
        <table className="ctx-table">
          <thead>
            <tr>
              <th className="num">turn</th>
              <th className="num">line</th>
              <th>time</th>
              <th>model</th>
              <th className="num">input</th>
              <th className="num">cache_cr</th>
              <th className="num">cache_rd</th>
              <th className="num">output</th>
              <th className="num">ctx</th>
              <th className="num">% cap</th>
              <th className="num">Δ</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, idx) => {
              const pct = (r.ctx / cap) * 100;
              const sev = pct > 90 ? 'over' : pct > 75 ? 'warn' : pct > 50 ? 'mid' : '';
              return (
                <tr key={idx}
                  className={(idx === hoverIdx ? 'hi ' : '') + sev}
                  onMouseEnter={() => setHoverIdx(idx)}
                  onMouseLeave={() => setHoverIdx(null)}>
                  <td className="num mono">{r.turnNum}</td>
                  <td className="num mono dim">{r.line}</td>
                  <td className="mono dim">{window.shortTime(r.ts)}</td>
                  <td className="mono">{shortModel(r.model)}</td>
                  <td className="num mono">{window.humanFmt(r.input)}</td>
                  <td className="num mono dim">{r.cacheCreate ? window.humanFmt(r.cacheCreate) : '—'}</td>
                  <td className="num mono dim">{r.cacheRead ? window.humanFmt(r.cacheRead) : '—'}</td>
                  <td className="num mono">{r.output ? window.humanFmt(r.output) : '—'}</td>
                  <td className="num mono">{window.humanFmt(r.ctx)}</td>
                  <td className={'num mono pct ' + sev}>{pct.toFixed(1)}%</td>
                  <td className={'num mono ' + (r.delta == null ? 'dim' : r.delta >= 0 ? 'pos' : 'neg')}>
                    {r.delta == null ? '—' : (r.delta >= 0 ? '+' : '') + window.humanFmt(r.delta)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SummaryStat({ label, value }) {
  return (
    <div className="ctx-stat">
      <div className="ctx-stat-label">{label}</div>
      <div className="ctx-stat-value mono">{value}</div>
    </div>
  );
}

function shortModel(m) {
  if (!m) return '?';
  // Canonical short name shared with the dashboard ("claude-fable-5[1m]"
  // → "fable-5"). The old regex here only knew opus/sonnet/haiku.
  return window.shortModelName ? window.shortModelName(m) : m;
}

function ContextChart({ rows, cap, hoverIdx, setHoverIdx }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(900);
  const h = 240;

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  const padL = 60, padR = 20, padT = 28, padB = 30;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  const peakCtx = Math.max(...rows.map(r => r.ctx));
  const yMaxAbs = Math.min(cap * 1.05, Math.max(peakCtx * 1.10, cap * 0.10));
  const xScale = i => padL + (rows.length === 1 ? plotW/2 : (i / (rows.length - 1)) * plotW);
  const yScale = v => padT + plotH - (v / yMaxAbs) * plotH;

  function niceTicks(maxV, n = 4) {
    if (maxV <= 0) return [0];
    const step0 = maxV / n;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const niceStep = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const arr = [];
    for (let v = 0; v <= maxV; v += niceStep) arr.push(v);
    return arr;
  }
  const yTicks = niceTicks(yMaxAbs, 4);

  const linePts = rows.map((r, i) => `${xScale(i)},${yScale(r.ctx)}`).join(' ');
  const fillPts = `M ${padL},${padT + plotH} L ` +
    rows.map((r, i) => `${xScale(i)},${yScale(r.ctx)}`).join(' L ') +
    ` L ${xScale(rows.length - 1)},${padT + plotH} Z`;

  function onMove(e) {
    const rect = ref.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    if (mx < padL || mx > w - padR) { setHoverIdx(null); return; }
    const frac = (mx - padL) / plotW;
    let idx = Math.round(frac * (rows.length - 1));
    if (idx < 0) idx = 0;
    if (idx >= rows.length) idx = rows.length - 1;
    setHoverIdx(idx);
  }

  return (
    <div ref={ref} style={{ position: 'relative', minHeight: h, marginTop: 4 }}
      onMouseMove={onMove} onMouseLeave={() => setHoverIdx(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        {/* Y grid */}
        {yTicks.map((v, i) => (
          <line key={'g'+i} x1={padL} x2={w - padR}
            y1={yScale(v)} y2={yScale(v)}
            stroke="#2a2c44" strokeOpacity="0.6" />
        ))}
        {/* Cap line */}
        {cap <= yMaxAbs && (
          <g>
            <line x1={padL} x2={w - padR} y1={yScale(cap)} y2={yScale(cap)}
              stroke="#ff5577" strokeWidth="1" strokeDasharray="3,3" strokeOpacity="0.85" />
            <text x={w - padR - 4} y={yScale(cap) - 4} fontSize="10"
              fill="#ff5577" textAnchor="end" fontFamily="var(--mono)">
              {window.humanFmt(cap)} cap
            </text>
          </g>
        )}
        {/* 75% / 50% reference */}
        {[0.5, 0.75].map(p => (
          <g key={'p'+p}>
            <line x1={padL} x2={w - padR} y1={yScale(cap*p)} y2={yScale(cap*p)}
              stroke="#7e84a3" strokeWidth="0.7" strokeDasharray="2,4" strokeOpacity="0.5" />
            <text x={padL + 4} y={yScale(cap*p) - 3} fontSize="9"
              fill="#7e84a3" fontFamily="var(--mono)">
              {(p*100).toFixed(0)}%
            </text>
          </g>
        ))}
        {/* Fill + line */}
        <path d={fillPts} fill="#00d4aa" fillOpacity="0.10" />
        <polyline points={linePts} stroke="#00d4aa" strokeWidth="1.6" fill="none" />
        {/* Dots */}
        {rows.map((r, i) => {
          const x = xScale(i), y = yScale(r.ctx);
          const sel = i === hoverIdx;
          return (
            <circle key={i} cx={x} cy={y} r={sel ? 4 : 1.8}
              fill={sel ? '#00d4aa' : '#00d4aa'}
              stroke={sel ? '#0c0d12' : 'none'} strokeWidth="1.5" />
          );
        })}
        {/* Hover crosshair */}
        {hoverIdx != null && (
          <line x1={xScale(hoverIdx)} x2={xScale(hoverIdx)}
            y1={padT} y2={padT + plotH}
            stroke="#fff" strokeOpacity="0.25" strokeDasharray="2,3" />
        )}

        {/* Y labels */}
        {yTicks.map((v, i) => (
          <text key={'yl'+i} x={padL - 6} y={yScale(v) + 3}
            fontSize="9.5" fill="#7e84a3" textAnchor="end" fontFamily="var(--mono)">
            {window.humanFmt(v)}
          </text>
        ))}
        {/* X label */}
        <text x={padL} y={h - 6} fontSize="9.5" fill="#7e84a3" fontFamily="var(--mono)">
          turn 1
        </text>
        <text x={w - padR} y={h - 6} fontSize="9.5" fill="#7e84a3"
          textAnchor="end" fontFamily="var(--mono)">
          turn {rows.length}
        </text>
      </svg>

      {/* Tooltip */}
      {hoverIdx != null && (() => {
        const r = rows[hoverIdx];
        const x = xScale(hoverIdx);
        const left = Math.min(w - 220, Math.max(padL, x + 10));
        return (
          <div style={{
            position: 'absolute',
            left, top: padT + 6,
            background: 'rgba(8,10,18,0.96)',
            border: '1px solid #00d4aa',
            borderRadius: 4, padding: '8px 10px',
            fontFamily: 'var(--mono)', fontSize: 11,
            color: '#e6e8f0', pointerEvents: 'none',
            whiteSpace: 'nowrap', zIndex: 5,
            boxShadow: '0 6px 20px rgba(0,0,0,0.6)',
          }}>
            <div style={{ color: '#00d4aa', fontWeight: 700, marginBottom: 4 }}>
              turn {r.turnNum} · line {r.line}
            </div>
            <Tip k="ctx"      v={window.humanFmt(r.ctx) + ' (' + (r.ctx/cap*100).toFixed(1) + '% cap)'} />
            <Tip k="input"    v={window.humanFmt(r.input)} />
            <Tip k="cache_cr" v={r.cacheCreate ? window.humanFmt(r.cacheCreate) : '—'} />
            <Tip k="cache_rd" v={r.cacheRead ? window.humanFmt(r.cacheRead) : '—'} />
            <Tip k="output"   v={r.output ? window.humanFmt(r.output) : '—'} />
            <Tip k="Δ"        v={r.delta == null ? '—' : (r.delta >= 0 ? '+' : '') + window.humanFmt(r.delta)} />
          </div>
        );
      })()}
    </div>
  );
}

function Tip({ k, v }) {
  return (
    <div style={{ display: 'flex', gap: 12, justifyContent: 'space-between', lineHeight: 1.5 }}>
      <span style={{ color: '#7e84a3' }}>{k}</span>
      <span style={{ fontWeight: 600 }}>{v}</span>
    </div>
  );
}

window.ContextGrowthView = ContextGrowthView;
