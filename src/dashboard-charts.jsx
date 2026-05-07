// Dashboard chart components — interactive SVG (with hover tooltips).
// Six time-series cards, two horizontal bars, and the burn-rate panel.

const TH = {
  bgDark:  '#1a1a2e',
  bgAxes:  '#16213e',
  border:  '#2a2a4a',
  text:    '#e0e0e0',
  textDim: '#8888aa',
  grid:    '#2a2a4a',
};

const COL = {
  inputTokens:       '#00d4aa',
  outputTokens:      '#ff8c42',
  cacheCreateTokens: '#aa55ff',
  cacheReadTokens:   '#ff3366',
  totalTokens:       '#00d4ff',
  costUSD:           '#ffdd00',
};

const MODEL_COLORS = {
  'opus-4-7':   '#ff2222',
  'opus-4-6':   '#ff8800',
  'opus-4-5':   '#ffdd00',
  'sonnet-4-6': '#00bbff',
  'sonnet-4-5': '#8866ff',
  'haiku-4-5':  '#88cc44',
  '<synthetic>':'#888888',
};

function humanFmt(v, isCurrency) {
  const prefix = isCurrency ? '$' : '';
  const abs = Math.abs(v);
  let out;
  if (abs >= 1e9) out = (v / 1e9).toFixed(2).replace(/\.?0+$/, '') + 'B';
  else if (abs >= 1e6) out = (v / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
  else if (abs >= 1e3) out = (v / 1e3).toFixed(1).replace(/\.?0+$/, '') + 'K';
  else if (isCurrency) out = v.toFixed(2);
  else out = String(Math.round(v));
  return prefix + out;
}

// Currency formatter that scales precision with magnitude — Schwabish:
// drop decimals readers can't act on. $11357.99 → $11.4K; $789.62 → $790;
// $5.32 → $5.32. Cents kept only when the amount is small enough that
// they actually matter.
function humanCurrency(v) {
  const abs = Math.abs(v);
  if (abs >= 1e9) return '$' + (v / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
  if (abs >= 1e6) return '$' + (v / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
  if (abs >= 1e3) return '$' + (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
  if (abs >= 100) return '$' + Math.round(v);
  if (abs >= 10)  return '$' + v.toFixed(1).replace(/\.0$/, '');
  return '$' + v.toFixed(2);
}

function fmtDate(ts, opts = {}) {
  const d = new Date(ts);
  const M = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  if (opts.month) return M[d.getUTCMonth()] + ' ' + d.getUTCFullYear();
  if (opts.day) return M[d.getUTCMonth()] + ' ' + d.getUTCDate();
  if (opts.full) return `${M[d.getUTCMonth()]} ${String(d.getUTCDate()).padStart(2,'0')} ${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}`;
  return d.toISOString();
}

// --- Tooltip primitive (positioned in container, follows the cursor) ---
// Flips left/up when it would overflow the viewport right/bottom edges.
function Tooltip({ tip }) {
  const ref = React.useRef(null);
  const [pos, setPos] = React.useState({ left: 0, top: 0, ready: false });
  React.useLayoutEffect(() => {
    if (!tip || !ref.current) return;
    const el = ref.current;
    const w = el.offsetWidth, h = el.offsetHeight;
    const parentRect = el.offsetParent ? el.offsetParent.getBoundingClientRect() : { left: 0, top: 0 };
    const margin = 8;
    // Default: lower-right of cursor
    let left = tip.x + 12;
    let top  = tip.y + 12;
    // Absolute viewport position the tooltip would occupy
    const absRight  = parentRect.left + left + w;
    const absBottom = parentRect.top  + top  + h;
    if (absRight  > window.innerWidth  - margin) left = tip.x - w - 12;
    if (absBottom > window.innerHeight - margin) top  = tip.y - h - 12;
    // Don't overflow LEFT/TOP edges of the viewport either
    const minLeft = -parentRect.left + margin;
    const minTop  = -parentRect.top  + margin;
    if (left < minLeft) left = minLeft;
    if (top  < minTop)  top  = minTop;
    setPos({ left, top, ready: true });
  }, [tip]);

  if (!tip) return null;
  const style = {
    position: 'absolute',
    left: pos.left,
    top: pos.top,
    visibility: pos.ready ? 'visible' : 'hidden',
    background: 'rgba(8, 10, 18, 0.96)',
    border: '1px solid ' + (tip.accent || TH.border),
    borderRadius: 4,
    padding: '8px 10px',
    fontFamily: 'monospace',
    fontSize: 11,
    color: TH.text,
    pointerEvents: 'none',
    whiteSpace: 'nowrap',
    boxShadow: '0 6px 20px rgba(0,0,0,0.6)',
    zIndex: 5,
    maxWidth: 280,
  };
  return (
    <div ref={ref} style={style}>
      {tip.title && <div style={{ color: tip.accent || TH.text, fontWeight: 700, marginBottom: 4 }}>{tip.title}</div>}
      {(tip.lines || []).map((l, i) => (
        <div key={i} style={{ display: 'flex', gap: 8, justifyContent: 'space-between', lineHeight: 1.6 }}>
          <span style={{ color: TH.textDim }}>{l[0]}</span>
          <span style={{ color: l[2] || TH.text, fontWeight: 600 }}>{l[1]}</span>
        </div>
      ))}
    </div>
  );
}

// --- Time-series panel ---
function TimeSeriesPanel({ title, events, valueKey, color, isCurrency, range, binMs }) {
  const ref = React.useRef(null);
  const [size, setSize] = React.useState({ w: 600, h: 280 });
  const [tip, setTip] = React.useState(null);

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => {
      const r = es[0].contentRect;
      setSize({ w: r.width, h: r.height });
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  const { w, h } = size;
  const padL = 50, padR = 50, padT = 28, padB = 28;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  const bins = [];
  let bStart = range.start;
  let i = 0;
  while (bStart < range.end) {
    const bEnd = bStart + binMs;
    let sum = 0;
    let count = 0;
    while (i < events.length && events[i].ts < bEnd) {
      sum += events[i][valueKey] || 0;
      count++;
      i++;
    }
    bins.push({ start: bStart, end: bEnd, sum, count });
    bStart = bEnd;
  }

  const maxBin = Math.max(1, ...bins.map(b => b.sum));
  // Cumulative line — start anchored at (range.start, 0) so the line
  // visually originates at the left edge of the plot, not at the end
  // of the first bin (the previous behavior left a leading gap).
  const cumPts = [{ ts: range.start, v: 0, binIdx: -1 }];
  let ci = 0, runEv = 0;
  for (let k = 0; k < bins.length; k++) {
    const upTo = bins[k].end;
    while (ci < events.length && events[ci].ts < upTo) {
      runEv += events[ci][valueKey] || 0;
      ci++;
    }
    cumPts.push({ ts: upTo, v: runEv, binIdx: k });
  }
  const total = runEv;
  const maxCum = Math.max(1, total);

  const xScale = ts => padL + ((ts - range.start) / (range.end - range.start)) * plotW;
  const yBar = v => padT + plotH - (v / maxBin) * plotH;
  const yCum = v => padT + plotH - (v / maxCum) * plotH;

  const ticks = [];
  const startD = new Date(range.start);
  let m = startD.getUTCMonth(), y = startD.getUTCFullYear();
  for (let it = 0; it < 24; it++) {
    const t = Date.UTC(y, m, 1);
    if (t > range.start && t < range.end) ticks.push(t);
    m++; if (m > 11) { m = 0; y++; }
  }

  function niceTicks(maxV, n = 4) {
    const step0 = maxV / n;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const niceStep = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const arr = [];
    for (let v = 0; v <= maxV; v += niceStep) arr.push(v);
    return arr;
  }
  const yTicksL = niceTicks(maxBin);
  const yTicksR = niceTicks(maxCum);

  const barW = Math.max(1, (plotW / bins.length) * 0.9);

  // Mouse tracking — find nearest bin
  function onMouseMove(e) {
    const rect = ref.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null);
      return;
    }
    const frac = (mx - padL) / plotW;
    const ts = range.start + frac * (range.end - range.start);
    let idx = Math.floor((ts - range.start) / binMs);
    if (idx < 0) idx = 0;
    if (idx >= bins.length) idx = bins.length - 1;
    const b = bins[idx];
    const cum = cumPts[idx + 1];  // +1 to skip the leading (range.start, 0) anchor
    setTip({
      x: mx, y: my,
      title: `${fmtDate(b.start, {day:true})} – ${fmtDate(b.end, {day:true})}`,
      accent: color,
      lines: [
        ['period',     humanFmt(b.sum, isCurrency)],
        ['cumulative', humanFmt(cum ? cum.v : 0, isCurrency)],
        ['requests',   String(b.count)],
        ['% of total', total > 0 ? ((b.sum / total) * 100).toFixed(2) + '%' : '0%'],
      ],
    });
  }

  return (
    <div ref={ref} style={{
      background: TH.bgAxes, border: `1px solid ${TH.border}`,
      borderRadius: 4, padding: 0, position: 'relative', minHeight: 220,
    }}
    onMouseMove={onMouseMove}
    onMouseLeave={() => setTip(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        {yTicksL.map((v, idx) => (
          <line key={'g'+idx} x1={padL} x2={w - padR}
            y1={yBar(v)} y2={yBar(v)}
            stroke={TH.grid} strokeOpacity="0.3" strokeWidth="1" />
        ))}
        {bins.map((b, idx) => {
          const x = xScale(b.start);
          const y = yBar(b.sum);
          const isHover = tip && Math.floor((tip.x - padL) / plotW * (range.end - range.start) / binMs) === idx;
          return (
            <rect key={idx} x={x} y={y} width={barW} height={Math.max(0, padT + plotH - y)}
              fill={color} fillOpacity={isHover ? 0.85 : 0.3} />
          );
        })}
        <polygon points={
          [`${padL},${padT + plotH}`,
           ...cumPts.map(p => `${xScale(p.ts)},${yCum(p.v)}`),
           `${xScale(range.end)},${padT + plotH}`].join(' ')
        } fill={color} fillOpacity="0.04" />
        <polyline points={cumPts.map(p => `${xScale(p.ts)},${yCum(p.v)}`).join(' ')}
          stroke="#fff" strokeOpacity="0.15" strokeWidth="4" fill="none" />
        <polyline points={cumPts.map(p => `${xScale(p.ts)},${yCum(p.v)}`).join(' ')}
          stroke={color} strokeWidth="2" fill="none" />

        {/* Hover crosshair */}
        {tip && (
          <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
            stroke={color} strokeOpacity="0.4" strokeWidth="1" strokeDasharray="2,3" />
        )}

        {yTicksL.map((v, idx) => (
          <text key={'yl'+idx} x={padL - 6} y={yBar(v) + 4}
            fontSize="9" fill={TH.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt(v, isCurrency)}
          </text>
        ))}
        {yTicksR.map((v, idx) => (
          <text key={'yr'+idx} x={w - padR + 6} y={yCum(v) + 4}
            fontSize="9" fill={TH.textDim} textAnchor="start" fontFamily="monospace">
            {humanFmt(v, isCurrency)}
          </text>
        ))}
        {ticks.map((t, idx) => (
          <text key={'x'+idx} x={xScale(t)} y={h - padB + 14}
            fontSize="9" fill={TH.textDim} textAnchor="middle" fontFamily="monospace">
            {fmtDate(t, { month: true })}
          </text>
        ))}

        <text x={w/2} y={18} fontSize="13" fontWeight="bold" fill={TH.text}
          textAnchor="middle" fontFamily="monospace">{title}</text>

        <text x={12} y={padT + plotH/2} fontSize="9" fill={TH.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 12 ${padT + plotH/2})`}>per 1d</text>
        <text x={w - 12} y={padT + plotH/2} fontSize="9" fill={TH.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 ${w - 12} ${padT + plotH/2})`}>cumulative</text>

        <g>
          <rect x={w - padR - 92} y={padT + 2} width={86} height={20} rx={4}
            fill={TH.bgAxes} stroke={color} strokeOpacity="0.8" />
          <text x={w - padR - 49} y={padT + 16} fontSize="11" fontWeight="bold"
            fill={color} textAnchor="middle" fontFamily="monospace">
            Total: {humanFmt(total, isCurrency)}
          </text>
        </g>
      </svg>
      <Tooltip tip={tip} />
    </div>
  );
}

// --- Horizontal bar chart ---
function HBar({ title, rows, totalForPct, fmt, fixedColors }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(600);
  const [hover, setHover] = React.useState(null);
  const [mouse, setMouse] = React.useState({ x: 0, y: 0 });

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  const h = 40 + rows.length * 44;
  // Dynamic left pad: fits the widest label at 11px monospace
  // (~6.6px/char), clamped so the bar still has room.
  const FONT_CHAR_PX = 6.6;
  const longestLabelChars = rows.reduce((m, r) => Math.max(m, (r.label || '').length), 0);
  const padL = Math.min(
    Math.max(60, w * 0.45),
    Math.ceil(longestLabelChars * FONT_CHAR_PX) + 16
  );
  const padR = 60, padT = 32, padB = 18;
  const plotW = Math.max(10, w - padL - padR);
  const max = Math.max(1, ...rows.map(r => r.value));
  const xMax = max * 1.4;

  const total = rows.reduce((a, r) => a + r.value, 0);

  function rowTip(r) {
    if (hover == null) return null;
    const c = (fixedColors && fixedColors[r.label]) || r.color || COL.inputTokens;
    return {
      x: mouse.x, y: mouse.y, title: r.label, accent: c,
      lines: [
        ['value',    fmt ? fmt(r) : humanFmt(r.value)],
        ['% of bar', total > 0 ? ((r.value / total) * 100).toFixed(2) + '%' : '0%'],
        ...(totalForPct ? [['% of total', ((r.value / totalForPct) * 100).toFixed(2) + '%']] : []),
      ],
    };
  }

  return (
    <div ref={ref} style={{
      background: TH.bgAxes, border: `1px solid ${TH.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
    }}
    onMouseMove={e => {
      const rect = ref.current.getBoundingClientRect();
      setMouse({ x: e.clientX - rect.left, y: e.clientY - rect.top });
    }}
    onMouseLeave={() => setHover(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        <text x={w/2} y={20} fontSize="13" fontWeight="bold" fill={TH.text}
          textAnchor="middle" fontFamily="monospace">{title}</text>
        {rows.map((r, idx) => {
          const y = padT + idx * 36;
          const barW = (r.value / xMax) * plotW;
          const c = (fixedColors && fixedColors[r.label]) || r.color || COL.inputTokens;
          const pct = totalForPct ? ` (${(r.value / totalForPct * 100).toFixed(1)}%)` : '';
          const isHover = hover === idx;
          return (
            <g key={idx}
              onMouseEnter={() => setHover(idx)}
              style={{ cursor: 'pointer' }}>
              <rect x={0} y={y} width={w} height={32} fill="transparent" />
              <text x={padL - 8} y={y + 18} fontSize="11" fill={TH.text}
                textAnchor="end" fontFamily="monospace">{r.label}</text>
              <rect x={padL} y={y + 4} width={Math.max(2, barW)} height={26}
                fill={c} fillOpacity={isHover ? 1 : 0.85}
                stroke={isHover ? '#fff' : 'none'} strokeOpacity={0.5} />
              <text x={padL + barW + 8} y={y + 22} fontSize="11" fontWeight="bold"
                fill={TH.text} fontFamily="monospace">
                {fmt ? fmt(r) : humanFmt(r.value)}{pct}
              </text>
            </g>
          );
        })}
      </svg>
      {hover != null && <Tooltip tip={rowTip(rows[hover])} />}
    </div>
  );
}

// --- Burn rate panel ---
function BurnRatePanel({ events, sessions, totalSessions, limitHits, range, windowBoundaries }) {
  const ref = React.useRef(null);
  const [size, setSize] = React.useState({ w: 1200, h: 360 });
  const [tip, setTip] = React.useState(null);

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => {
      const r = es[0].contentRect;
      setSize({ w: r.width, h: r.height });
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const { w, h } = size;
  // Top is just title (no legend); bottom has x-tick labels + the legend.
  const padL = 60, padR = 30, padT = 30, padB = 56;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  // EMA + polyline rendering assume time-sorted sessions; the backend
  // returns them in cost-desc order, so re-sort by midpoint ascending.
  const sortedSessions = sessions.slice().sort((a, b) => {
    const am = (a.start + a.end) / 2;
    const bm = (b.start + b.end) / 2;
    return am - bm;
  });
  const sessionData = sortedSessions.map((s, i) => {
    const dur = (s.end - s.start) / 3600000;
    const durH = Math.max(dur, 1/60);
    const sums = { input: 0, output: 0, cc: 0, cr: 0, cost: 0 };
    const modelCounts = {};
    for (const e of s.events) {
      sums.input += e.input_tokens;
      sums.output += e.output_tokens;
      sums.cc += e.cache_create;
      sums.cr += e.cache_read;
      sums.cost += e.cost_usd;
      modelCounts[e.model] = (modelCounts[e.model] || 0) + 1;
    }
    let primary = 'opus-4-6', max = 0;
    for (const [m, c] of Object.entries(modelCounts)) if (c > max) { max = c; primary = m; }
    return {
      idx: i,
      start: s.start, end: s.end,
      mid: (s.start + s.end) / 2,
      durH,
      reqs: (s.requests != null) ? s.requests : s.events.length,
      ctxEnd: s.ctxEnd || 0,
      primary,
      sums,
      out_per_h:    sums.output / durH,
      input_per_h:  sums.input / durH,
      cc_per_h:     sums.cc / durH,
      cr_per_h:     sums.cr / durH,
    };
  });

  function ema(arr, alpha = 0.15) {
    if (!arr.length) return [];
    const out = [arr[0]];
    for (let i = 1; i < arr.length; i++) out.push(alpha * arr[i] + (1 - alpha) * out[i-1]);
    return out;
  }

  const series = {
    output: { color: '#ee4444', label: 'Output', vals: ema(sessionData.map(s => s.out_per_h)) },
    input:  { color: '#44dd66', label: 'Input',  vals: ema(sessionData.map(s => s.input_per_h)) },
    cc:     { color: '#dd66aa', label: 'Cache Create', vals: ema(sessionData.map(s => s.cc_per_h)) },
    cr:     { color: '#44bbbb', label: 'Cache Read',   vals: ema(sessionData.map(s => s.cr_per_h)) },
  };

  // Densify each EMA line: linearly interpolate between session midpoints
  // so hit-testing works along the whole curve, not just at session points.
  const DENSE_STEPS = 32; // sub-points per segment
  function densify(vals) {
    const dense = [];
    if (sessionData.length === 0) return dense;
    if (sessionData.length === 1) {
      dense.push({ ts: sessionData[0].mid, val: vals[0], srcIdx: 0, t: 0 });
      return dense;
    }
    for (let i = 0; i < sessionData.length - 1; i++) {
      const a = sessionData[i], b = sessionData[i + 1];
      const va = vals[i], vb = vals[i + 1];
      for (let s = 0; s < DENSE_STEPS; s++) {
        const t = s / DENSE_STEPS;
        dense.push({
          ts: a.mid + (b.mid - a.mid) * t,
          val: va + (vb - va) * t,
          srcIdx: t < 0.5 ? i : i + 1,
          t,
        });
      }
    }
    const last = sessionData.length - 1;
    dense.push({ ts: sessionData[last].mid, val: vals[last], srcIdx: last, t: 0 });
    return dense;
  }
  const densified = {};
  for (const k of Object.keys(series)) densified[k] = densify(series[k].vals);

  let allRates = [];
  for (const k of Object.keys(series)) allRates = allRates.concat(series[k].vals);
  for (const s of sessionData) allRates.push(s.out_per_h);
  allRates = allRates.filter(v => v > 0);
  const yMin = Math.max(100, Math.min(...allRates) * 0.3);
  const yMax = Math.max(...allRates) * 3;
  const logYMin = Math.log10(yMin), logYMax = Math.log10(yMax);
  const xScale = ts => padL + ((ts - range.start) / (range.end - range.start)) * plotW;
  const yScale = v => {
    const cv = Math.max(yMin * 0.1, v);
    return padT + plotH - ((Math.log10(cv) - logYMin) / (logYMax - logYMin)) * plotH;
  };

  const yTicks = [];
  for (let p = Math.ceil(logYMin); p <= Math.floor(logYMax); p++) yTicks.push(Math.pow(10, p));

  const xTicks = [];
  const startD = new Date(range.start);
  let mn = startD.getUTCMonth(), yr = startD.getUTCFullYear();
  for (let it = 0; it < 24; it++) {
    const t = Date.UTC(yr, mn, 1);
    if (t > range.start && t < range.end) xTicks.push(t);
    mn++; if (mn > 11) { mn = 0; yr++; }
  }

  // Find nearest session dot to cursor
  function onMove(e) {
    const rect = ref.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    let best = null, bestD = 1e9;
    for (const s of sessionData) {
      const sx = xScale(s.mid), sy = yScale(s.out_per_h);
      const d = Math.hypot(sx - mx, sy - my);
      if (d < bestD) { bestD = d; best = s; }
    }
    // Also check rate limits (vertical bands)
    let nearLimit = null;
    for (const lh of limitHits) {
      const lx = xScale(lh.ts);
      if (Math.abs(lx - mx) < 5) nearLimit = lh;
    }
    if (nearLimit) {
      setTip({ x: mx, y: my, title: 'Rate limit hit', accent: '#ff3366',
        lines: [['when', fmtDate(nearLimit.ts, {full:true}) + ' UTC']] });
      return;
    }
    // Check proximity to EMA lines (output/input/cache create/cache read).
    // Use the densified curves so hover works along the whole line, not
    // only where session points exist.
    if (sessionData.length > 0) {
      let bestSeriesKey = null, bestSeriesD = 1e9, bestPoint = null;
      for (const k of Object.keys(series)) {
        const dense = densified[k];
        for (const p of dense) {
          const px = xScale(p.ts);
          if (Math.abs(px - mx) > 30) continue; // cheap reject
          const py = yScale(p.val);
          const d = Math.hypot(px - mx, py - my);
          if (d < bestSeriesD) { bestSeriesD = d; bestSeriesKey = k; bestPoint = p; }
        }
      }
      // Prefer line over dot when line is significantly closer
      const dotD = best ? Math.hypot(xScale(best.mid)-mx, yScale(best.out_per_h)-my) : 1e9;
      if (bestSeriesKey && bestSeriesD < 14 && bestSeriesD < dotD - 4) {
        const sk = series[bestSeriesKey];
        const sAtCol = sessionData[bestPoint.srcIdx];
        const raw = {
          output: sAtCol.out_per_h,
          input:  sAtCol.input_per_h,
          cc:     sAtCol.cc_per_h,
          cr:     sAtCol.cr_per_h,
        }[bestSeriesKey];
        setTip({
          x: mx, y: my,
          title: sk.label + ' (EMA)',
          accent: sk.color,
          lines: [
            ['nearest sess', '#' + (sAtCol.idx + 1) + ' / ' + sessionData.length],
            ['when',         fmtDate(bestPoint.ts, {full:true})],
            ['model',        sAtCol.primary],
            ['EMA tok/hr',   humanFmt(bestPoint.val)],
            ['raw tok/hr',   humanFmt(raw)],
          ],
        });
        return;
      }
    }
    if (best && bestD < 30) {
      setTip({
        x: mx, y: my,
        title: 'Session ' + (best.idx + 1),
        accent: MODEL_COLORS[best.primary] || '#888',
        lines: [
          ['model',         best.primary],
          ['start',         fmtDate(best.start, {full:true})],
          ['duration',      best.durH < 1 ? (best.durH*60).toFixed(0)+'m' : best.durH.toFixed(1)+'h'],
          ['requests',      String(best.reqs)],
          ...(best.ctxEnd > 0 ? [['ctx at end', humanFmt(best.ctxEnd)]] : []),
          ['out tok/hr',    humanFmt(best.out_per_h)],
          ['cache rd tok/hr', humanFmt(best.cr_per_h)],
          ['est. cost',     '$' + best.sums.cost.toFixed(2)],
        ],
      });
    } else {
      setTip(null);
    }
  }

  return (
    <div ref={ref} style={{
      background: TH.bgAxes, border: `1px solid ${TH.border}`,
      borderRadius: 4, padding: 0, height: 380, position: 'relative',
    }}
    onMouseMove={onMove}
    onMouseLeave={() => setTip(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        <text x={w/2} y={20} fontSize="14" fontWeight="bold" fill={TH.text}
          textAnchor="middle" fontFamily="monospace">
          Session Burn Rate  |  {fmtDate(range.start, {day:true})} – {fmtDate(range.end, {day:true})}, {new Date(range.end).getUTCFullYear()} UTC  |  {(totalSessions != null ? totalSessions : (events.reduce((s,e)=>s+(e.session_count||0),0) || sessions.length)).toLocaleString()} sessions, {events.reduce((s,e)=>s+(e.requests==null?1:e.requests),0).toLocaleString()} requests
        </text>
        {windowBoundaries.map((wb, i) => (
          <line key={'wb'+i} x1={xScale(wb)} x2={xScale(wb)}
            y1={padT} y2={padT + plotH}
            stroke="#fff" strokeOpacity="0.1" strokeWidth="1" strokeDasharray="2,3" />
        ))}
        {yTicks.map((v, i) => (
          <line key={'yg'+i} x1={padL} x2={w-padR}
            y1={yScale(v)} y2={yScale(v)}
            stroke={TH.grid} strokeOpacity="0.25" />
        ))}
        {yTicks.map((v, i) => (
          <text key={'yl'+i} x={padL - 8} y={yScale(v) + 4}
            fontSize="10" fill={TH.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt(v)}
          </text>
        ))}
        {sessionData.map((s, i) => {
          // Scale dot AREA by ctx-at-end-of-session.
          //   100k ctx → 25 area-pts²,  1M ctx → 250 area-pts²
          // Falls back to duration scaling (ccusage_plot.py:801) when
          // ctxEnd isn't known (synth/live mode).
          const areaPts2 = s.ctxEnd > 0
            ? Math.min(Math.max(s.ctxEnd / 4000, 25), 250)
            : Math.min(Math.max(s.durH * 60, 25), 250);
          const r = Math.sqrt(areaPts2);
          const isHover = tip && tip.title === 'Session ' + (s.idx + 1);
          return (
            <circle key={'sd'+i} cx={xScale(s.mid)} cy={yScale(s.out_per_h)}
              r={isHover ? r + 2 : r} fill={MODEL_COLORS[s.primary] || '#888'}
              fillOpacity={isHover ? 0.95 : 0.5}
              stroke={isHover ? '#fff' : '#fff'}
              strokeOpacity={isHover ? 0.9 : 0.3}
              strokeWidth={isHover ? 1.5 : 0.5} />
          );
        })}
        {Object.entries(series).map(([k, s]) => {
          const pts = densified[k].map(p => `${xScale(p.ts)},${yScale(p.val)}`).join(' ');
          return <polyline key={k} points={pts}
            stroke={s.color} strokeWidth="1.5" fill="none" strokeOpacity="0.85" />;
        })}
        {limitHits.map((lh, i) => (
          <line key={'lh'+i} x1={xScale(lh.ts)} x2={xScale(lh.ts)}
            y1={padT} y2={padT + plotH}
            stroke="#ff3366" strokeWidth="2" strokeOpacity="0.7" />
        ))}
        {xTicks.map((t, i) => (
          <text key={'x'+i} x={xScale(t)} y={h - padB + 14}
            fontSize="10" fill={TH.textDim} textAnchor="middle" fontFamily="monospace">
            {fmtDate(t, { month: true })}
          </text>
        ))}
        <text x={14} y={padT + plotH/2} fontSize="10" fill={TH.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 14 ${padT + plotH/2})`}>Tokens / hour (EMA)</text>

        <g transform={`translate(${padL + 20}, ${h - 22})`}>
          {Object.entries(series).map(([k, s], i) => (
            <g key={k} transform={`translate(${i * 130}, 0)`}>
              <line x1={0} x2={20} y1={6} y2={6} stroke={s.color} strokeWidth="2" />
              <text x={26} y={10} fontSize="10" fill={TH.text} fontFamily="monospace">{s.label} (EMA)</text>
            </g>
          ))}
          <g transform={`translate(${4 * 130}, 0)`}>
            <line x1={0} x2={20} y1={6} y2={6} stroke="#ff3366" strokeWidth="2" />
            <text x={26} y={10} fontSize="10" fill={TH.text} fontFamily="monospace">Rate limit hit</text>
          </g>
        </g>
      </svg>
      <Tooltip tip={tip} />
    </div>
  );
}

window.TimeSeriesPanel = TimeSeriesPanel;
window.HBar = HBar;
window.BurnRatePanel = BurnRatePanel;
window.dashboardTheme = TH;
window.dashboardCol = COL;
window.modelColors = MODEL_COLORS;
window.humanFmt = humanFmt;
window.humanCurrency = humanCurrency;
window.fmtDate = fmtDate;
