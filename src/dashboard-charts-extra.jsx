// Extra dashboard panels: Cache TTL split and Per-Session Context Growth.
// Loaded after dashboard-charts.jsx; depends on its globals (TH/COL/humanFmt/fmtDate).

const TH_X       = window.dashboardTheme;
const COL_X      = window.dashboardCol;
const humanFmt_X = window.humanFmt;
const fmtDate_X  = window.fmtDate;

// ──────────────────────────────────────────────────────────────────────
// Cache TTL panel — stacked 5m + 1h hourly bars, with 5m share strip
// ──────────────────────────────────────────────────────────────────────

function CacheTTLPanel({ events, range, binMs }) {
  const ref = React.useRef(null);
  const [size, setSize] = React.useState({ w: 1200, h: 320 });
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
  const padL = 60, padR = 60, padT = 50, padB = 36;
  const sharePctH = 56;        // bottom strip
  const gap = 6;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(40, h - padT - padB - sharePctH - gap);

  // Trim x-range to where data actually exists (with small pad).
  // Otherwise a synthetic 3-month range with two days of real data shows
  // 90% empty space.
  const dataRange = React.useMemo(() => {
    const tsList = events
      .filter(e => (e.ephemeral_5m || 0) + (e.ephemeral_1h || 0) > 0)
      .map(e => e.ts);
    if (!tsList.length) return range;
    const dMin = Math.min(...tsList);
    const dMax = Math.max(...tsList);
    const span = Math.max(dMax - dMin, 60_000);
    const pad = span * 0.04;
    return { start: dMin - pad, end: dMax + pad };
  }, [events, range.start, range.end]);

  // Auto pick bin size: aim for ~70 bins across whatever range we plot.
  // Falls back to dashboard-wide binMs when that's denser. Daily / 6h / 1h
  // / 15m steps, snapped to clean boundaries.
  const adaptiveBin = React.useMemo(() => {
    const span = dataRange.end - dataRange.start;
    const target = span / 70;
    const stepMs = [
      15 * 60_000,           // 15m
      30 * 60_000,           // 30m
      60 * 60_000,           // 1h
      3 * 60 * 60_000,       // 3h
      6 * 60 * 60_000,       // 6h
      12 * 60 * 60_000,      // 12h
      24 * 60 * 60_000,      // 1d
      3 * 24 * 60 * 60_000,  // 3d
      7 * 24 * 60 * 60_000,  // 7d
    ];
    let chosen = stepMs[stepMs.length - 1];
    for (const s of stepMs) {
      if (s >= target) { chosen = s; break; }
    }
    return Math.min(chosen, binMs); // never coarser than dashboard binMs
  }, [dataRange.start, dataRange.end, binMs]);

  const useRange = dataRange;
  const useBin = adaptiveBin;

  // Build bins, snapping start to bin boundary so labels read cleanly
  const bins = React.useMemo(() => {
    const arr = [];
    let bStart = Math.floor(useRange.start / useBin) * useBin;
    const end = Math.ceil(useRange.end / useBin) * useBin;
    const sorted = events.slice().sort((a, b) => a.ts - b.ts);
    let i = 0;
    while (i < sorted.length && sorted[i].ts < bStart) i++;
    while (bStart < end) {
      const bEnd = bStart + useBin;
      let s5 = 0, s1 = 0, n = 0;
      while (i < sorted.length && sorted[i].ts < bEnd) {
        s5 += sorted[i].ephemeral_5m || 0;
        s1 += sorted[i].ephemeral_1h || 0;
        n++; i++;
      }
      arr.push({ start: bStart, end: bEnd, s5, s1, n });
      bStart = bEnd;
    }
    return arr;
  }, [events, useRange.start, useRange.end, useBin]);

  let total5 = 0, total1 = 0, maxBin = 1;
  for (const b of bins) {
    total5 += b.s5;
    total1 += b.s1;
    const t = b.s5 + b.s1;
    if (t > maxBin) maxBin = t;
  }

  const xScale = ts => padL + ((ts - useRange.start) / (useRange.end - useRange.start)) * plotW;
  const yBar = v => padT + plotH - (v / maxBin) * plotH;
  const barW = Math.max(2, (plotW / Math.max(1, bins.length)) * 0.9);

  // y ticks
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
  const yTicks = niceTicks(maxBin);

  // x ticks (months)
  const xTicks = [];
  const startD = new Date(useRange.start);
  let mn = startD.getUTCMonth(), yr = startD.getUTCFullYear();
  for (let it = 0; it < 24; it++) {
    const t = Date.UTC(yr, mn, 1);
    if (t > useRange.start && t < useRange.end) xTicks.push(t);
    mn++; if (mn > 11) { mn = 0; yr++; }
  }

  // Share strip Y origin
  const shareTop = padT + plotH + gap;
  const shareBot = shareTop + sharePctH;

  // Median + p95 of share %
  const sharePct = bins.map(b => {
    const t = b.s5 + b.s1;
    return t > 0 ? (b.s5 / t) * 100 : null;
  });
  const validShares = sharePct.filter(v => v !== null).sort((a, b) => a - b);
  const median = validShares.length ? validShares[Math.floor(validShares.length / 2)] : null;
  const p95 = validShares.length ? validShares[Math.floor(validShares.length * 0.95)] : null;

  function shareY(p) { return shareBot - (p / 100) * sharePctH; }

  function onMove(e) {
    const rect = ref.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > shareBot) {
      setTip(null); return;
    }
    const frac = (mx - padL) / plotW;
    const ts = useRange.start + frac * (useRange.end - useRange.start);
    let idx = Math.floor((ts - bins[0].start) / useBin);
    if (idx < 0) idx = 0;
    if (idx >= bins.length) idx = bins.length - 1;
    const b = bins[idx];
    const tot = b.s5 + b.s1;
    const pct = tot > 0 ? (b.s5 / tot) * 100 : 0;
    setTip({
      x: mx, y: my,
      title: `${fmtDate_X(b.start, {day:true})}`,
      accent: COL_X.cacheCreateTokens,
      lines: [
        ['ephemeral 5m', humanFmt_X(b.s5)],
        ['ephemeral 1h', humanFmt_X(b.s1)],
        ['total',        humanFmt_X(tot)],
        ['5m share',     tot > 0 ? pct.toFixed(1) + '%' : '—'],
        ['records',      String(b.n)],
      ],
    });
  }

  // Find peak bin for annotation
  let peakIdx = -1, peakVal = 0;
  for (let i = 0; i < bins.length; i++) {
    const t = bins[i].s5 + bins[i].s1;
    if (t > peakVal) { peakVal = t; peakIdx = i; }
  }

  const grandTotal = total5 + total1;
  const sharePctOverall = grandTotal > 0 ? (total5 / grandTotal) * 100 : 0;

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative', minHeight: 320,
    }}
    onMouseMove={onMove}
    onMouseLeave={() => setTip(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        {/* Title */}
        <text x={w/2} y={20} fontSize="14" fontWeight="bold" fill={TH_X.text}
          textAnchor="middle" fontFamily="monospace">
          Prompt-Cache TTL Split
        </text>
        <text x={w/2} y={36} fontSize="10" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace">
          {bins.length.toLocaleString()} bins · 5m {humanFmt_X(total5)} · 1h {humanFmt_X(total1)} · 5m share {sharePctOverall.toFixed(1)}%
        </text>

        {/* Y grid */}
        {yTicks.map((v, i) => (
          <line key={'g'+i} x1={padL} x2={w - padR}
            y1={yBar(v)} y2={yBar(v)}
            stroke={TH_X.grid} strokeOpacity="0.3" />
        ))}

        {/* Stacked bars: 1h on bottom, 5m on top */}
        {bins.map((b, idx) => {
          if (b.s5 + b.s1 <= 0) return null;
          const x = xScale(b.start);
          const y1 = yBar(b.s1);                    // top of 1h
          const y5 = yBar(b.s5 + b.s1);             // top of 5m
          const h1 = padT + plotH - y1;             // 1h bar height
          const h5 = y1 - y5;                       // 5m bar height
          const isPeak = idx === peakIdx;
          return (
            <g key={'bar'+idx}>
              <rect x={x} y={y1} width={barW} height={Math.max(0, h1)}
                fill={COL_X.cacheCreateTokens} fillOpacity={isPeak ? 0.95 : 0.85} />
              <rect x={x} y={y5} width={barW} height={Math.max(0, h5)}
                fill={COL_X.inputTokens} fillOpacity={isPeak ? 0.95 : 0.85} />
            </g>
          );
        })}

        {/* Crosshair */}
        {tip && (
          <line x1={tip.x} x2={tip.x} y1={padT} y2={shareBot}
            stroke="#fff" strokeOpacity="0.25" strokeWidth="1" strokeDasharray="2,3" />
        )}

        {/* Y-axis labels */}
        {yTicks.map((v, i) => (
          <text key={'yl'+i} x={padL - 6} y={yBar(v) + 4}
            fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt_X(v)}
          </text>
        ))}

        {/* Top-panel y label */}
        <text x={14} y={padT + plotH/2} fontSize="9" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 14 ${padT + plotH/2})`}>cache_create / bin</text>

        {/* Legend */}
        <g transform={`translate(${padL + 8}, ${padT + 12})`}>
          <rect x={0} y={0} width={12} height={12} fill={COL_X.inputTokens} fillOpacity="0.85" />
          <text x={18} y={10} fontSize="10" fill={TH_X.text} fontFamily="monospace">ephemeral_5m</text>
          <rect x={120} y={0} width={12} height={12} fill={COL_X.cacheCreateTokens} fillOpacity="0.85" />
          <text x={138} y={10} fontSize="10" fill={TH_X.text} fontFamily="monospace">ephemeral_1h</text>
        </g>

        {/* Share strip background */}
        <rect x={padL} y={shareTop} width={plotW} height={sharePctH}
          fill="#0f1428" fillOpacity="0.6" />

        {/* Share strip: one continuous line+area that linearly
            interpolates across empty bins (no cache_create activity)
            so sparse data still reads as a single trace. */}
        {(() => {
          const valid = [];
          for (let i = 0; i < bins.length; i++) {
            const v = sharePct[i];
            if (v !== null) valid.push({ x: xScale(bins[i].start) + barW / 2, y: shareY(v) });
          }
          if (valid.length < 2) return null;
          const fill = `M ${valid[0].x},${shareBot} ` +
            valid.map(p => `L ${p.x},${p.y}`).join(' ') +
            ` L ${valid[valid.length-1].x},${shareBot} Z`;
          const line = `M ` + valid.map(p => `${p.x},${p.y}`).join(' L ');
          return (
            <g>
              <path d={fill} fill={COL_X.inputTokens} fillOpacity="0.20" />
              <path d={line} stroke={COL_X.inputTokens} strokeWidth="1.2" fill="none" />
            </g>
          );
        })()}

        {/* Reference lines on share strip */}
        {median !== null && (
          <g>
            <line x1={padL} x2={w - padR} y1={shareY(median)} y2={shareY(median)}
              stroke={TH_X.textDim} strokeWidth="0.8" strokeOpacity="0.6" strokeDasharray="3,3" />
            <text x={w - padR - 4} y={shareY(median) - 3} fontSize="9"
              fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              median {median.toFixed(0)}%
            </text>
          </g>
        )}
        {p95 !== null && (
          <g>
            <line x1={padL} x2={w - padR} y1={shareY(p95)} y2={shareY(p95)}
              stroke={TH_X.textDim} strokeWidth="0.8" strokeOpacity="0.6" strokeDasharray="3,3" />
            <text x={w - padR - 4} y={shareY(p95) - 3} fontSize="9"
              fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              p95 {p95.toFixed(0)}%
            </text>
          </g>
        )}

        {/* Share strip y labels */}
        {[0, 50, 100].map((p, i) => (
          <text key={'sy'+i} x={padL - 6} y={shareY(p) + 3}
            fontSize="8" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
            {p}%
          </text>
        ))}
        <text x={14} y={shareTop + sharePctH/2} fontSize="9" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 14 ${shareTop + sharePctH/2})`}>5m share</text>

        {/* X-axis labels (under share strip) */}
        {xTicks.map((t, i) => (
          <text key={'x'+i} x={xScale(t)} y={shareBot + 14}
            fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
            {fmtDate_X(t, { month: true })}
          </text>
        ))}

        {/* Strip border */}
        <rect x={padL} y={shareTop} width={plotW} height={sharePctH}
          fill="none" stroke={TH_X.border} strokeOpacity="0.6" />
      </svg>
      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// Per-Session Context Growth panel
// ──────────────────────────────────────────────────────────────────────

const CTX_TURN_CAP = Infinity;
const MODEL_CAPS = {
  'opus-4-7':   1_000_000,
  'opus-4-6':   1_000_000,
  'opus-4-5':   1_000_000,
  'sonnet-4-6':   200_000,
  'sonnet-4-5':   200_000,
  'haiku-4-5':    200_000,
};
function capForModel(m) {
  return MODEL_CAPS[m] || (String(m).toLowerCase().includes('opus') ? 1_000_000 : 200_000);
}

function buildSessionTurns(events) {
  // Group events by session_id, sort by turn_index (real turn boundaries
  // computed in txToDashData) or ts as fallback, and emit per-turn ctx sizes.
  const bySess = new Map();
  for (const e of events) {
    const sid = e.session_id || 'unknown';
    if (!bySess.has(sid)) bySess.set(sid, []);
    bySess.get(sid).push(e);
  }
  const out = {};
  for (const [sid, evs] of bySess) {
    evs.sort((a, b) => {
      if (a.turn_index != null && b.turn_index != null) return a.turn_index - b.turn_index;
      return a.ts - b.ts;
    });
    const counts = {};
    for (const e of evs) counts[e.model] = (counts[e.model] || 0) + 1;
    let dom = 'unknown', max = 0;
    for (const [m, c] of Object.entries(counts)) if (c > max) { max = c; dom = m; }
    const seq = evs.map((e, i) => ({
      t: e.turn_index != null ? e.turn_index : i,
      ctx: (e.input_tokens || 0) + (e.cache_create || 0) + (e.cache_read || 0),
    }));
    if (!out[dom]) out[dom] = [];
    out[dom].push({ id: sid, seq });
  }
  return out;
}

function perTurnStats(sessions) {
  if (!sessions || !sessions.length) return { turns: [], median: [], p90: [], count: [], maxT: 0 };
  const byTurn = new Map();
  for (const s of sessions) {
    for (const p of s.seq) {
      if (p.t >= CTX_TURN_CAP) break;
      if (!byTurn.has(p.t)) byTurn.set(p.t, []);
      byTurn.get(p.t).push(p.ctx);
    }
  }
  if (!byTurn.size) return { turns: [], median: [], p90: [], count: [], maxT: 0 };
  const maxT = Math.max(...byTurn.keys());
  const turns = [], median = [], p90 = [], count = [];
  for (let t = 0; t <= maxT; t++) {
    const vals = byTurn.get(t);
    if (!vals || vals.length < 1) {
      turns.push(t); median.push(null); p90.push(null); count.push(0);
      continue;
    }
    vals.sort((a, b) => a - b);
    turns.push(t);
    median.push(vals[Math.floor(vals.length / 2)]);
    const p90idx = Math.min(vals.length - 1, Math.floor(vals.length * 0.9));
    p90.push(vals[p90idx]);
    count.push(vals.length);
  }
  return { turns, median, p90, count, maxT };
}

function ContextSubPanel({ title, sessions, color, cap, w, h }) {
  const ref = React.useRef(null);
  const [tip, setTip] = React.useState(null);

  const padL = 50, padR = 16, padT = 38, padB = 24;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  const { turns, median, p90, count, maxT } = React.useMemo(() => perTurnStats(sessions), [sessions]);
  const nSess = sessions.length;
  const longest = sessions.reduce((m, s) => Math.max(m, s.seq.length), 0);
  let maxCtx = 0;
  for (const s of sessions) for (const p of s.seq) if (p.ctx > maxCtx) maxCtx = p.ctx;

  // Dynamic x-domain: 0 → this model's longest turn (rounded up nicely)
  const xMax = Math.max(1, maxT);
  const yMax = Math.min(cap * 1.05, Math.max(maxCtx * 1.10, cap * 0.10));
  const xScale = t => padL + (t / xMax) * plotW;
  const yScale = v => padT + plotH - (v / yMax) * plotH;

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
  const yTicks = niceTicks(yMax, 4);
  // Dynamic x-ticks based on this panel's max turn
  function xTickValues(maxV, n = 6) {
    if (maxV <= 0) return [0];
    const step0 = maxV / n;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const arr = [];
    for (let v = 0; v <= maxV; v += step) arr.push(Math.round(v));
    if (arr[arr.length - 1] !== maxV && (maxV - arr[arr.length - 1]) / step > 0.4) arr.push(maxV);
    return arr;
  }
  const xTicks = xTickValues(xMax);

  // Per-session faint traces — alpha scales with count
  const traceAlpha = 0.6;

  // Hit test on hover: find nearest session line at that x
  function onMove(e) {
    const rect = ref.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    const turn = Math.round(((mx - padL) / plotW) * xMax);
    if (turn < 0 || turn > xMax) { setTip(null); return; }
    const med = median[turn];
    const p9 = p90[turn];
    const liveCount = count[turn] || 0;
    setTip({
      x: mx, y: my,
      title: `turn ${turn}`,
      accent: color,
      lines: [
        ['median ctx', med !== null && med !== undefined ? humanFmt_X(med) : '—'],
        ['p90 ctx',    p9 !== null && p9 !== undefined  ? humanFmt_X(p9)  : '—'],
        ['sessions @ turn', `${liveCount} / ${nSess}`],
        ['cap',        humanFmt_X(cap)],
      ],
    });
  }

  // Build "sessions still active" area (faint, behind curves)
  const maxCount = Math.max(1, ...count);
  const countAreaH = plotH * 0.18; // bottom 18% of plot
  function countY(c) {
    return padT + plotH - (c / maxCount) * countAreaH;
  }

  return (
    <div ref={ref} style={{ position: 'relative', flex: 1, minWidth: 0 }}
      onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        <text x={padL} y={18} fontSize="11" fontWeight="bold" fill={color}
          fontFamily="monospace">{title}</text>
        <text x={padL} y={32} fontSize="9" fill={TH_X.textDim}
          fontFamily="monospace">
          {nSess} sessions · longest: {longest} · max ctx: {humanFmt_X(maxCtx)}
        </text>

        {/* Mini legend (top-right of panel) */}
        <g transform={`translate(${w - padR - 190}, 14)`}>
          <rect x={0} y={0} width={14} height={8} fill={color} fillOpacity="0.18" />
          <text x={18} y={7} fontSize="8.5" fill={TH_X.textDim} fontFamily="monospace">active</text>
          <line x1={52} x2={66} y1={4} y2={4} stroke={color} strokeWidth="0.7" strokeOpacity="0.6" />
          <text x={70} y={7} fontSize="8.5" fill={TH_X.textDim} fontFamily="monospace">sessions</text>
          <line x1={108} x2={122} y1={4} y2={4} stroke="#fff" strokeWidth="1.8" />
          <text x={126} y={7} fontSize="8.5" fill={TH_X.text} fontFamily="monospace">median</text>
          <line x1={158} x2={172} y1={4} y2={4} stroke="#c8ccd9" strokeWidth="1" strokeDasharray="3,3" />
          <text x={176} y={7} fontSize="8.5" fill={TH_X.textDim} fontFamily="monospace">p90</text>
        </g>

        {/* Y grid */}
        {yTicks.map((v, i) => (
          <line key={'g'+i} x1={padL} x2={w - padR}
            y1={yScale(v)} y2={yScale(v)}
            stroke={TH_X.grid} strokeOpacity="0.25" />
        ))}

        {/* Cap line */}
        {cap <= yMax && (
          <g>
            <line x1={padL} x2={w - padR} y1={yScale(cap)} y2={yScale(cap)}
              stroke="#ff5577" strokeWidth="1" strokeDasharray="2,3" strokeOpacity="0.7" />
            <text x={w - padR - 4} y={yScale(cap) - 3} fontSize="8.5"
              fill="#ff5577" textAnchor="end" fontFamily="monospace">
              {humanFmt_X(cap)} cap
            </text>
          </g>
        )}

        {/* Per-session traces */}
        {sessions.map((s, i) => {
          if (s.seq.length < 2) return null;
          const pts = [];
          for (const p of s.seq) {
            if (p.t >= CTX_TURN_CAP) break;
            pts.push(`${xScale(p.t)},${yScale(Math.min(p.ctx, yMax))}`);
          }
          if (pts.length < 2) return null;
          return (
            <polyline key={'s'+i} points={pts.join(' ')}
              stroke={color} strokeWidth="0.7" strokeOpacity={traceAlpha} fill="none" />
          );
        })}

        {/* p90 line */}
        {(() => {
          const pts = [];
          for (let i = 0; i < turns.length; i++) {
            if (p90[i] === null || p90[i] === undefined) continue;
            pts.push(`${xScale(turns[i])},${yScale(Math.min(p90[i], yMax))}`);
          }
          return pts.length > 1 ? (
            <polyline points={pts.join(' ')} stroke="#c8ccd9" strokeWidth="1.8"
              strokeDasharray="4,3" fill="none" />
          ) : null;
        })()}

        {/* Median line */}
        {(() => {
          const pts = [];
          for (let i = 0; i < turns.length; i++) {
            if (median[i] === null || median[i] === undefined) continue;
            pts.push(`${xScale(turns[i])},${yScale(Math.min(median[i], yMax))}`);
          }
          return pts.length > 1 ? (
            <polyline points={pts.join(' ')} stroke="#ffffff" strokeWidth="1.8"
              fill="none" />
          ) : null;
        })()}

        {/* Crosshair */}
        {tip && (
          <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
            stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
        )}

        {/* Y labels */}
        {yTicks.map((v, i) => (
          <text key={'yl'+i} x={padL - 6} y={yScale(v) + 3}
            fontSize="8.5" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt_X(v)}
          </text>
        ))}
        {/* X labels */}
        {xTicks.map((t, i) => (
          <text key={'x'+i} x={xScale(t)} y={padT + plotH + 14}
            fontSize="8.5" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
            {t}
          </text>
        ))}
      </svg>
      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}

// Canonicalize backend model strings (e.g. "claude-opus-4-7-20251101") to
// the short keys used by `window.modelColors` ("opus-4-7"). Falls back to
// the original string when no canonical short name applies.
function shortModelName(m) {
  if (!m) return 'unknown';
  let s = String(m).toLowerCase();
  if (s.startsWith('claude-')) s = s.slice('claude-'.length);
  // Strip trailing -YYYYMMDD date
  s = s.replace(/-\d{8}$/, '');
  return s;
}

function ContextGrowthPanel({ events, realSessions }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  // Prefer real per-session ctx traces from the backend when present;
  // fall back to bucket-grouping the synth/live events. The
  // pseudo-model "<synthetic>" is dropped — it's a synthetic
  // resampling row from the parser, not a real model.
  const byModel = React.useMemo(() => {
    const dropKey = (k) => k === '<synthetic>' || k === 'synthetic';
    if (realSessions && realSessions.length) {
      const out = {};
      for (const s of realSessions) {
        const turns = (s.turns || []).map(t => ({ t: t.t, ctx: t.ctx }));
        if (!turns.length) continue;
        const key = shortModelName(s.model);
        if (dropKey(key)) continue;
        if (!out[key]) out[key] = [];
        out[key].push({ id: s.session_id, seq: turns });
      }
      return out;
    }
    const m = buildSessionTurns(events);
    for (const k of Object.keys(m)) if (dropKey(k)) delete m[k];
    return m;
  }, [events, realSessions]);

  // Models present, sorted by session count desc. This drives both the
  // checkbox row and the per-model sub-panels.
  const models = React.useMemo(() =>
    Object.entries(byModel)
      .map(([m, ss]) => ({ model: m, count: ss.length }))
      .sort((a, b) => b.count - a.count)
  , [byModel]);

  // Selection = top 2 by session count, with explicit user toggles
  // layered on top. This avoids the "first synth-mode set sticks
  // through realSessions arrival" bug — the default tracks current
  // models without needing a reset effect.
  const [overrides, setOverrides] = React.useState({});
  const sel = React.useMemo(() => {
    const s = new Set(models.slice(0, 2).map(m => m.model));
    for (const [m, on] of Object.entries(overrides)) {
      if (on) s.add(m); else s.delete(m);
    }
    return s;
  }, [models, overrides]);

  function toggle(m) {
    setOverrides(prev => ({ ...prev, [m]: !sel.has(m) }));
  }

  const cellW = Math.max(280, (w - 16) / 2);
  const cellH = 230;
  const cmpW = w;
  const cmpH = 240;

  // Pair sub-panels into rows of 2.
  const rows = [];
  for (let i = 0; i < models.length; i += 2) rows.push(models.slice(i, i + 2));

  // Models actually drawn in the comparison overlay.
  const cmpModels = models.filter(m => sel.has(m.model));

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
    }}>
      {/* Header */}
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}` }}>
        <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
          Per-Session Context Growth
        </div>
        <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
          context size = input + cache_create + cache_read · x = turn within session
        </div>
      </div>

      {/* Model checkbox row — defaults to top 2 by session count. */}
      <div style={{
        padding: '8px 14px', borderBottom: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '6px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
      }}>
        <span style={{ color: TH_X.textDim }}>compare:</span>
        {models.map(m => {
          const c = (window.modelColors && window.modelColors[m.model]) || '#888';
          const checked = sel.has(m.model);
          return (
            <label key={m.model} style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              cursor: 'pointer', userSelect: 'none',
              opacity: checked ? 1 : 0.6,
            }}>
              <input type="checkbox" checked={checked} onChange={() => toggle(m.model)}
                style={{ accentColor: c, margin: 0 }} />
              <span style={{ width: 10, height: 10, background: c, display: 'inline-block', borderRadius: 2 }} />
              <span style={{ color: TH_X.text, fontWeight: 600 }}>{m.model}</span>
              <span style={{ color: TH_X.textDim }}>({m.count})</span>
            </label>
          );
        })}
        {!models.length && <span>no sessions in range</span>}
      </div>

      {/* Comparison overlay — driven by checked models */}
      <ComparisonRow models={cmpModels} byModel={byModel} w={cmpW} h={cmpH} />

      {/* Per-model sub-panels (rows of 2) for every model with data */}
      {rows.map((rowModels, ri) => (
        <div key={ri} style={{
          display: 'flex', gap: 0,
          borderTop: ri > 0 ? `1px solid ${TH_X.border}` : 'none',
        }}>
          {rowModels.map(m => {
            const sessions = byModel[m.model] || [];
            let maxCtx = 0;
            for (const s of sessions) for (const p of s.seq) if (p.ctx > maxCtx) maxCtx = p.ctx;
            const cap = m.model.includes('opus') ? 1_000_000 : 200_000;
            const color = (window.modelColors && window.modelColors[m.model]) || '#888';
            return (
              <ContextSubPanel key={m.model} title={m.model} sessions={sessions}
                color={color} cap={Math.max(cap, maxCtx * 1.05)} w={cellW} h={cellH} />
            );
          })}
        </div>
      ))}
    </div>
  );
}

function ComparisonRow({ models, byModel, w, h }) {
  const ref = React.useRef(null);
  const [tip, setTip] = React.useState(null);

  const padL = 60, padR = 30, padT = 50, padB = 24;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  // One stats bundle per checked model, in the same order as `models`.
  const series = React.useMemo(() => models.map(m => {
    const sessions = byModel[m.model] || [];
    return { model: m.model, count: sessions.length, stats: perTurnStats(sessions) };
  }), [models, byModel]);

  // Adaptive cap: 1M when any opus is in the comparison, else 200k. Then
  // expand if the data exceeds it.
  let observedMax = 0;
  for (const s of series) for (const v of s.stats.p90) if (v && v > observedMax) observedMax = v;
  const baseCap = series.some(s => s.model.includes('opus')) ? 1_000_000 : 200_000;
  const cap = Math.max(baseCap, observedMax * 1.05);
  const yMax = cap * 1.05;
  // Dynamic x-domain: max turn across all checked models
  const xMax = Math.max(1, ...series.map(s => s.stats.maxT || 0));
  const xScale = t => padL + (t / xMax) * plotW;
  const yScale = v => padT + plotH - (v / yMax) * plotH;

  function yTickValues(maxV, n = 5) {
    if (maxV <= 0) return [0];
    const step0 = maxV / n;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const arr = [];
    for (let v = 0; v <= maxV; v += step) arr.push(v);
    return arr;
  }
  const yTicks = yTickValues(cap, 5);
  function xTickValues(maxV, n = 6) {
    if (maxV <= 0) return [0];
    const step0 = maxV / n;
    const exp = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / exp;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * exp;
    const arr = [];
    for (let v = 0; v <= maxV; v += step) arr.push(Math.round(v));
    if (arr[arr.length - 1] !== maxV && (maxV - arr[arr.length - 1]) / step > 0.4) arr.push(maxV);
    return arr;
  }
  const xTicks = xTickValues(xMax);

  function buildLine(turns, vals) {
    const pts = [];
    for (let i = 0; i < turns.length; i++) {
      if (vals[i] === null || vals[i] === undefined) continue;
      pts.push(`${xScale(turns[i])},${yScale(Math.min(vals[i], yMax))}`);
    }
    return pts.join(' ');
  }

  function onMove(e) {
    const rect = ref.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    const turn = Math.round(((mx - padL) / plotW) * xMax);
    if (turn < 0 || turn > xMax) { setTip(null); return; }
    const fmt = v => v !== null && v !== undefined ? humanFmt_X(v) : '—';
    const lines = [];
    for (const s of series) {
      const live = s.stats.count[turn] || 0;
      lines.push([`${s.model} median`, fmt(s.stats.median[turn])]);
      lines.push([`${s.model} p90`,    fmt(s.stats.p90[turn])]);
      lines.push([`${s.model} active`, `${live} / ${s.count}`]);
    }
    setTip({ x: mx, y: my, title: `turn ${turn}`, accent: '#ffffff', lines });
  }

  const titleText = series.length === 0
    ? 'select models above to compare'
    : series.length === 1
      ? `${series[0].model}  ·  median + p90 per turn`
      : series.map(s => s.model).join(' vs ') + '  ·  median + p90 per turn';

  return (
    <div ref={ref} style={{ position: 'relative', borderBottom: `1px solid ${TH_X.border}` }}
      onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        <text x={padL} y={20} fontSize="11" fontWeight="bold" fill={TH_X.text}
          fontFamily="monospace">
          {titleText}
        </text>

        {/* Legend — one cluster per checked model. Wraps at edge. */}
        {(() => {
          // Painted width per cluster: ~245px of glyphs + 25px gutter.
          // Bumping from 230 prevents the next cluster's color swatch
          // from overlapping the previous cluster's "p90" label.
          const clusterW = 270;
          return series.map((s, i) => {
            const c = (window.modelColors && window.modelColors[s.model]) || '#888';
            const x = padL + (i * clusterW) % Math.max(1, plotW);
            const yRow = padT - 18 + Math.floor((i * clusterW) / Math.max(1, plotW)) * 14;
            return (
              <g key={s.model} transform={`translate(${x}, ${yRow})`}>
                <rect x={0} y={0} width={4} height={12} fill={c} />
                <text x={9} y={9} fontSize="9.5" fontWeight="700" fill={c} fontFamily="monospace">{s.model}</text>
                <line x1={86} x2={102} y1={5} y2={5} stroke={c} strokeWidth="2" />
                <text x={108} y={9} fontSize="9.5" fill={TH_X.text} fontFamily="monospace">
                  median ({s.count} sess)
                </text>
                <line x1={210} x2={226} y1={5} y2={5} stroke={c} strokeWidth="1" strokeDasharray="3,3" />
                <text x={232} y={9} fontSize="9.5" fill={TH_X.textDim} fontFamily="monospace">p90</text>
              </g>
            );
          });
        })()}

        {/* Y grid */}
        {yTicks.map((v, i) => (
          <line key={'g'+i} x1={padL} x2={w - padR}
            y1={yScale(v)} y2={yScale(v)}
            stroke={TH_X.grid} strokeOpacity="0.25" />
        ))}

        {/* Cap line */}
        <line x1={padL} x2={w - padR} y1={yScale(cap)} y2={yScale(cap)}
          stroke="#ff5577" strokeWidth="1" strokeDasharray="2,3" strokeOpacity="0.7" />
        <text x={padL - 6} y={yScale(cap) + 3} fontSize="9"
          fill="#ff5577" textAnchor="end" fontFamily="monospace">{humanFmt_X(cap)}</text>

        {/* Lines — p90 dashed under, median solid on top, per checked model */}
        {series.map(s => {
          const c = (window.modelColors && window.modelColors[s.model]) || '#888';
          return (
            <polyline key={'p90-'+s.model} points={buildLine(s.stats.turns, s.stats.p90)}
              stroke={c} strokeWidth="2" strokeDasharray="4,3" fill="none" />
          );
        })}
        {series.map(s => {
          const c = (window.modelColors && window.modelColors[s.model]) || '#888';
          return (
            <polyline key={'med-'+s.model} points={buildLine(s.stats.turns, s.stats.median)}
              stroke={c} strokeWidth="2" fill="none" />
          );
        })}

        {/* Crosshair */}
        {tip && (
          <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
            stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
        )}

        {/* Y labels */}
        {yTicks.map((v, i) => (
          <text key={'yl'+i} x={padL - 6} y={yScale(v) + 3}
            fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt_X(v)}
          </text>
        ))}
        {/* X labels */}
        {xTicks.map((t, i) => (
          <text key={'x'+i} x={xScale(t)} y={padT + plotH + 14}
            fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
            {t}
          </text>
        ))}
        <text x={14} y={padT + plotH/2} fontSize="9" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 14 ${padT + plotH/2})`}>context size</text>
        <text x={(padL + w - padR)/2} y={h - 4} fontSize="9" fill={TH_X.textDim}
          textAnchor="middle" fontFamily="monospace">turn number within session</text>
      </svg>
      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}


// Reusable tooltip primitive (the original Tooltip lives in a closure; expose ours).
// Flips left/up when it would overflow the viewport right/bottom edges.
function DashTooltip({ tip }) {
  const ref = React.useRef(null);
  const [pos, setPos] = React.useState({ left: 0, top: 0, ready: false });
  React.useLayoutEffect(() => {
    if (!tip || !ref.current) return;
    const el = ref.current;
    const w = el.offsetWidth, h = el.offsetHeight;
    const parentRect = el.offsetParent ? el.offsetParent.getBoundingClientRect() : { left: 0, top: 0 };
    const margin = 8;
    let left = tip.x + 12;
    let top  = tip.y + 12;
    const absRight  = parentRect.left + left + w;
    const absBottom = parentRect.top  + top  + h;
    if (absRight  > window.innerWidth  - margin) left = tip.x - w - 12;
    if (absBottom > window.innerHeight - margin) top  = tip.y - h - 12;
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
    border: '1px solid ' + (tip.accent || TH_X.border),
    borderRadius: 4,
    padding: '8px 10px',
    fontFamily: 'monospace',
    fontSize: 11,
    color: TH_X.text,
    pointerEvents: 'none',
    whiteSpace: 'nowrap',
    boxShadow: '0 6px 20px rgba(0,0,0,0.6)',
    zIndex: 5,
    maxWidth: 280,
  };
  return (
    <div ref={ref} style={style}>
      {tip.title && <div style={{ color: tip.accent || TH_X.text, fontWeight: 700, marginBottom: 4 }}>{tip.title}</div>}
      {(tip.lines || []).map((l, i) => (
        <div key={i} style={{ display: 'flex', gap: 12, justifyContent: 'space-between', lineHeight: 1.6 }}>
          <span style={{ color: TH_X.textDim }}>{l[0]}</span>
          <span style={{ color: l[2] || TH_X.text, fontWeight: 600 }}>{l[1]}</span>
        </div>
      ))}
    </div>
  );
}

window.CacheTTLPanel = CacheTTLPanel;
window.ContextGrowthPanel = ContextGrowthPanel;
window.DashTooltip = DashTooltip;
window.shortModelName = shortModelName;
