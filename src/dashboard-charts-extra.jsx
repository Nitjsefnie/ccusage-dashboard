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

  // Bucketing rule (matches backend _bucket_seconds + dashboard binMs):
  // pick the LARGEST bin in [60s, 1d] that yields ≥100 bins. Never
  // coarser than the dashboard-wide binMs.
  const adaptiveBin = React.useMemo(() => {
    const span = Math.max(1, dataRange.end - dataRange.start);
    const stepMs = [
      60_000,                // 1m
      5 * 60_000,            // 5m
      15 * 60_000,           // 15m
      30 * 60_000,           // 30m
      60 * 60_000,           // 1h
      6 * 60 * 60_000,       // 6h
      12 * 60 * 60_000,      // 12h
      24 * 60 * 60_000,      // 1d
    ];
    let chosen = stepMs[0];
    for (const s of stepMs) {
      if (span / s < 100) break;
      chosen = s;
    }
    return Math.min(chosen, binMs);
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

  // x ticks (adaptive: months / days / hours)
  const xTicks = window.timeTicksUTC(useRange.start, useRange.end);

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
            so sparse data still reads as a single trace. The line is
            also extended half a bucket past each end with a linear
            extrapolation of the share value, so the visual reaches
            the full plot width. */}
        {(() => {
          const valid = [];
          for (let i = 0; i < bins.length; i++) {
            const v = sharePct[i];
            if (v !== null) valid.push({ ts: bins[i].start, share: v });
          }
          if (valid.length < 2) return null;
          const binDur = bins[0].end - bins[0].start;
          const half = binDur / 2;
          const f = valid[0], s = valid[1];
          const l = valid[valid.length - 1], p2 = valid[valid.length - 2];
          const clampPct = v => Math.max(0, Math.min(100, v));
          const extended = [
            { ts: f.ts - half, share: clampPct(1.5 * f.share - 0.5 * s.share) },
            ...valid,
            { ts: l.ts + half, share: clampPct(1.5 * l.share - 0.5 * p2.share) },
          ];
          const xy = extended.map(p =>
            ({ x: xScale(p.ts) + barW / 2, y: shareY(p.share) })
          );
          const fill = `M ${xy[0].x},${shareBot} ` +
            xy.map(p => `L ${p.x},${p.y}`).join(' ') +
            ` L ${xy[xy.length-1].x},${shareBot} Z`;
          const line = `M ` + xy.map(p => `${p.x},${p.y}`).join(' L ');
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
          <text key={'x'+i} x={xScale(t.ts)} y={shareBot + 14}
            fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
            {t.label}
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
  'fable-5':    1_000_000,
  'opus-4-8':   1_000_000,
  'opus-4-7':   1_000_000,
  'opus-4-6':   1_000_000,
  'opus-4-5':   1_000_000,
  'sonnet-5':   1_000_000,
  'sonnet-4-6': 1_000_000,
  'sonnet-4-5':   200_000,
  'haiku-4-5':    200_000,
};
function capForModel(m) {
  const s = String(m).toLowerCase();
  return MODEL_CAPS[m]
    || ((s.includes('opus') || s.includes('fable')) ? 1_000_000 : 200_000);
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
    // Default behavior: every session has an implicit (turn 0, ctx 0)
    // origin. Real turns are 1-indexed off that.
    const seq = [{ t: 0, ctx: 0 }];
    evs.forEach((e, i) => {
      const t = (e.turn_index != null ? e.turn_index : i) + 1;
      // Per-call context window. Prefer the per-event `ctx` produced by
      // txToDashData (which respects usage.iterations max via
      // usageCtxInput); fall back to the input+create+read sum when an
      // older event-shape lacks it.
      seq.push({
        t,
        ctx: e.ctx != null
          ? e.ctx
          : (e.input_tokens || 0) + (e.cache_create || 0) + (e.cache_read || 0),
      });
    });
    if (!out[dom]) out[dom] = [];
    out[dom].push({ id: sid, seq });
  }
  return out;
}

function perTurnStats(sessions) {
  const empty = { turns: [], median: [], p25: [], p75: [], p90: [], count: [], maxT: 0 };
  if (!sessions || !sessions.length) return empty;
  const byTurn = new Map();
  for (const s of sessions) {
    for (const p of s.seq) {
      if (p.t >= CTX_TURN_CAP) break;
      if (!byTurn.has(p.t)) byTurn.set(p.t, []);
      byTurn.get(p.t).push(p.ctx);
    }
  }
  if (!byTurn.size) return empty;
  const maxT = Math.max(...byTurn.keys());
  const turns = [], median = [], p25 = [], p75 = [], p90 = [], count = [];
  const pick = (arr, q) => arr[Math.min(arr.length - 1, Math.floor(arr.length * q))];
  for (let t = 0; t <= maxT; t++) {
    const vals = byTurn.get(t);
    if (!vals || vals.length < 1) {
      turns.push(t);
      median.push(null); p25.push(null); p75.push(null); p90.push(null);
      count.push(0);
      continue;
    }
    vals.sort((a, b) => a - b);
    turns.push(t);
    median.push(vals[Math.floor(vals.length / 2)]);
    p25.push(pick(vals, 0.25));
    p75.push(pick(vals, 0.75));
    p90.push(pick(vals, 0.9));
    count.push(vals.length);
  }
  return { turns, median, p25, p75, p90, count, maxT };
}

function ContextSubPanel({ title, sessions, color, cap, w, h }) {
  const ref = React.useRef(null);
  const [tip, setTip] = React.useState(null);

  // Legend now sits below the plot, so padB grows (x-ticks + legend).
  const padL = 50, padR = 16, padT = 38, padB = 50;
  const plotW = Math.max(10, w - padL - padR);
  const plotH = Math.max(10, h - padT - padB);

  const { turns, median, p25, p75, p90, count, maxT } = React.useMemo(() => perTurnStats(sessions), [sessions]);
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
    const q1  = p25[turn];
    const q3  = p75[turn];
    const p9  = p90[turn];
    const fmtV = v => v !== null && v !== undefined ? humanFmt_X(v) : '—';
    const liveCount = count[turn] || 0;
    setTip({
      x: mx, y: my,
      title: `turn ${turn}`,
      accent: color,
      lines: [
        ['median ctx', fmtV(med)],
        ['p25–p75',    `${fmtV(q1)}–${fmtV(q3)}`],
        ['p90 ctx',    fmtV(p9)],
        ['files @ turn', `${liveCount} / ${nSess}`],
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
    <div ref={ref} style={{
      position: 'relative', flex: 1, minWidth: 0,
      border: `1px solid ${TH_X.border}`, borderRadius: 4,
      background: TH_X.bgAxes,
    }}
      onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        <text x={padL} y={18} fontSize="11" fontWeight="bold" fill={color}
          fontFamily="monospace">{title}</text>
        <text x={padL} y={32} fontSize="9" fill={TH_X.textDim}
          fontFamily="monospace">
          {nSess.toLocaleString()} agent files · longest: {longest} · max ctx: {humanFmt_X(maxCtx)}
        </text>

        {/* Mini legend, BELOW the plot */}
        <g transform={`translate(${padL}, ${h - 14})`}>
          <rect x={0} y={0} width={14} height={8} fill={color} fillOpacity="0.18" />
          <text x={18} y={7} fontSize="8.5" fill={TH_X.textDim} fontFamily="monospace">active</text>
          <line x1={52} x2={66} y1={4} y2={4} stroke={color} strokeWidth="0.7" strokeOpacity="0.6" />
          <text x={70} y={7} fontSize="8.5" fill={TH_X.textDim} fontFamily="monospace">sessions</text>
          <line x1={108} x2={122} y1={4} y2={4} stroke="#fff" strokeWidth="1.8" />
          <text x={126} y={7} fontSize="8.5" fill={TH_X.text} fontFamily="monospace">median</text>
          <rect x={158} y={1} width={14} height={6} fill={color} fillOpacity="0.4" />
          <text x={176} y={7} fontSize="8.5" fill={TH_X.textDim} fontFamily="monospace">p25–p75</text>
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

        {/* Sessions-still-active area (bottom strip) — the "active"
            legend swatch refers to this; it had stopped being drawn. */}
        {(() => {
          const pts = [];
          for (let i = 0; i < turns.length; i++) {
            pts.push(`${xScale(turns[i])},${countY(count[i] || 0)}`);
          }
          if (pts.length < 2) return null;
          const d = `M ${xScale(turns[0])},${padT + plotH} L ` + pts.join(' L ')
            + ` L ${xScale(turns[turns.length - 1])},${padT + plotH} Z`;
          return <path d={d} fill={color} fillOpacity="0.18" stroke="none" />;
        })()}

        {/* Per-session traces */}
        {sessions.map((s, i) => {
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

        {/* p25–p75 IQR ribbon (filled, model-colored, semi-transparent).
            Schwabish: show spread, not just upper-tail summary. */}
        {(() => {
          const top = [], bot = [];
          for (let i = 0; i < turns.length; i++) {
            const lo = p25[i], hi = p75[i];
            if (lo == null || hi == null) continue;
            top.push(`${xScale(turns[i])},${yScale(Math.min(hi, yMax))}`);
            bot.push(`${xScale(turns[i])},${yScale(Math.min(lo, yMax))}`);
          }
          if (top.length < 2) return null;
          const ribbon = `M ${top.join(' L ')} L ${bot.reverse().join(' L ')} Z`;
          return <path d={ribbon} fill={color} fillOpacity="0.35" stroke="none" />;
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

// Backend bucket projections center on bucket midpoint, so a polyline
// or band that just walks those midpoints leaves a half-bucket visual
// gap at each end (the data extends through [midpoint - N/2, midpoint
// + N/2) but the polyline only reaches the midpoint). This helper
// prepends + appends a virtual point half-a-bucket past each end with
// LINEARLY EXTRAPOLATED values (slope from the two adjacent points)
// so the rendered line/band fully covers the bucket extent.
//
// `valueKeys` lists the numeric fields to extrapolate. `log = true`
// extrapolates in log10 space (right for latency / response chars
// where the y-axis is log). `min` clamps the extrapolated value
// (default 0). Single-point series fall back to flat carry — no
// slope info available.
function extendBucketSeries(points, halfMs, valueKeys, options) {
  if (!points || !points.length) return points;
  const opts = options || {};
  const inLog = opts.log === true;
  const minY = opts.min !== undefined ? opts.min : 0;
  if (points.length === 1) {
    const p = points[0];
    return [{ ...p, ts: p.ts - halfMs }, p, { ...p, ts: p.ts + halfMs }];
  }
  const first = points[0], second = points[1];
  const last  = points[points.length - 1];
  const penul = points[points.length - 2];
  const lerp = (edge, neighbor) => {
    if (inLog) {
      const eps = 1e-9;
      const le = Math.log10(Math.max(eps, edge));
      const ln = Math.log10(Math.max(eps, neighbor));
      return Math.pow(10, 1.5 * le - 0.5 * ln);
    }
    return 1.5 * edge - 0.5 * neighbor;
  };
  const projFirst = { ...first };
  const projLast  = { ...last };
  for (const k of valueKeys) {
    projFirst[k] = Math.max(minY, lerp(first[k], second[k]));
    projLast[k]  = Math.max(minY, lerp(last[k],  penul[k]));
  }
  projFirst.ts = first.ts - halfMs;
  projLast.ts  = last.ts  + halfMs;
  return [projFirst, ...points, projLast];
}

// Canonicalize backend model strings (e.g. "claude-opus-4-7-20251101") to
// the short keys used by `window.modelColors` ("opus-4-7"). Falls back to
// the original string when no canonical short name applies.
function shortModelName(m) {
  if (!m) return 'unknown';
  let s = String(m).toLowerCase();
  if (s.startsWith('claude-')) s = s.slice('claude-'.length);
  // Strip trailing [variant] tag (e.g. "claude-fable-5[1m]"), then -YYYYMMDD date
  s = s.replace(/\[[^\]]*\]$/, '');
  s = s.replace(/-\d{8}$/, '');
  return s;
}

function ContextGrowthPanel({ events, realSessions, ctxTraces }) {
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
    // Preferred path: per-FILE ctx traces. Each file (main session OR
    // sub-agent invocation) is its own conversation with its own
    // dominant model. This makes models that only appear in sub-agent
    // calls (auto-compact, prompt-suggestion) visible in the panel
    // even when no main session JSONL exists.
    //
    // Index shift: backend turns are 0-indexed (first response = t0).
    // Re-index to 1-based and prepend an implicit (turn 0, ctx 0)
    // origin so every trace — including single-turn sub-agent calls
    // — has at least 2 points and renders as a polyline + contributes
    // a value-0 anchor to the per-turn median/p25/p75/p90 stats.
    if (ctxTraces && ctxTraces.length) {
      const out = {};
      for (const t of ctxTraces) {
        if (!t.turns || !t.turns.length) continue;
        const key = shortModelName(t.model);
        if (dropKey(key)) continue;
        const seq = [
          { t: 0, ctx: 0 },
          ...t.turns.map(p => ({ t: p.t + 1, ctx: p.ctx })),
        ];
        if (!out[key]) out[key] = [];
        out[key].push({ id: t.file_key || t.session_id, seq });
      }
      return out;
    }
    if (realSessions && realSessions.length) {
      const out = {};
      for (const s of realSessions) {
        if (!s.turns || !s.turns.length) continue;
        const seq = [
          { t: 0, ctx: 0 },
          ...s.turns.map(p => ({ t: p.t + 1, ctx: p.ctx })),
        ];
        const used = (s.models_used && s.models_used.length)
          ? s.models_used
          : [s.model];
        const seenKeys = new Set();
        for (const m of used) {
          const key = shortModelName(m);
          if (dropKey(key)) continue;
          if (seenKeys.has(key)) continue;
          seenKeys.add(key);
          if (!out[key]) out[key] = [];
          out[key].push({ id: s.session_id, seq });
        }
      }
      return out;
    }
    const m = buildSessionTurns(events);
    for (const k of Object.keys(m)) if (dropKey(k)) delete m[k];
    return m;
  }, [events, realSessions, ctxTraces]);

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

  // Two cells + 24px row padding + 12px gap + 2px border per cell.
  // (w-16)/2 overflowed the card by ~20px and showed up as horizontal
  // bleed whenever the window was resized.
  const cellW = Math.max(280, (w - 24 - 12 - 4) / 2);
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
      display: 'flex', flexDirection: 'column',
    }}>
      {/* Header */}
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, order: 1 }}>
        <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
          Per-Session Context Growth
        </div>
        <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
          context size = input + cache_create + cache_read · x = turn within session
        </div>
      </div>

      {/* Model checkbox row — directly below the comparison overlay
          (order 3, between the comparison at order 2 and sub-panel
          rows at default 0/4+). */}
      <div style={{
        padding: '8px 14px', borderBottom: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '6px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
        order: 3,
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
      <div style={{ order: 2 }}>
        <ComparisonRow models={cmpModels} byModel={byModel} w={cmpW} h={cmpH} />
      </div>

      {/* Per-model sub-panels (rows of 2) for every model with data.
          Each sub-panel renders its own border, so this just lays them
          out as a 2-column grid with gaps between cells. */}
      {rows.map((rowModels, ri) => (
        <div key={ri} style={{
          display: 'flex', gap: 12,
          padding: ri === 0 ? '12px 12px 6px' : '6px 12px',
          order: 4 + ri,
        }}>
          {rowModels.map(m => {
            const sessions = byModel[m.model] || [];
            let maxCtx = 0;
            for (const s of sessions) for (const p of s.seq) if (p.ctx > maxCtx) maxCtx = p.ctx;
            const cap = capForModel(m.model);
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

  // Legend now sits BELOW the plot, so padT shrinks (just title) and
  // padB grows (title-tick + legend).
  const padL = 60, padR = 30, padT = 30, padB = 60;
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
  const baseCap = Math.max(200_000, ...series.map(s => capForModel(s.model)));
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
      lines.push([`${s.model} active`, `${live} / ${s.count}`]);
    }
    setTip({ x: mx, y: my, title: `turn ${turn}`, accent: '#ffffff', lines });
  }

  const titleText = series.length === 0
    ? 'select models above to compare'
    : series.length === 1
      ? `${series[0].model}  ·  median per turn`
      : series.map(s => s.model).join(' vs ') + '  ·  median per turn';

  return (
    <div ref={ref} style={{ position: 'relative', borderBottom: `1px solid ${TH_X.border}` }}
      onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
      <svg width={w} height={h} style={{ display: 'block' }}>
        <text x={padL} y={20} fontSize="11" fontWeight="bold" fill={TH_X.text}
          fontFamily="monospace">
          {titleText}
        </text>

        {/* Legend — one cluster per checked model. Wraps at edge.
            Sits BELOW the plot area now (was above the title). */}
        {(() => {
          const clusterW = 270;
          const legendBaseY = padT + plotH + 30;  // below x-tick labels
          return series.map((s, i) => {
            const c = (window.modelColors && window.modelColors[s.model]) || '#888';
            const x = padL + (i * clusterW) % Math.max(1, plotW);
            const yRow = legendBaseY + Math.floor((i * clusterW) / Math.max(1, plotW)) * 14;
            return (
              <g key={s.model} transform={`translate(${x}, ${yRow})`}>
                <rect x={0} y={0} width={4} height={12} fill={c} />
                <text x={9} y={9} fontSize="9.5" fontWeight="700" fill={c} fontFamily="monospace">{s.model}</text>
                <line x1={86} x2={102} y1={5} y2={5} stroke={c} strokeWidth="2" />
                <text x={108} y={9} fontSize="9.5" fill={TH_X.text} fontFamily="monospace">
                  median ({s.count.toLocaleString()} files)
                </text>
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

        {/* Median line per checked model. p90 dropped — overlapping
            dashed lines for 2+ models read as noise, and per-model
            spread is already shown in the sub-panels below as IQR
            ribbons. */}
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
    borderColor: tip.accent || undefined,
    pointerEvents: 'none',
    zIndex: 5,
    width: 'max-content',
  };
  return (
    <div ref={ref} className="chart-tooltip" style={style}>
      {tip.title && (
        <div className="chart-tooltip-title" style={{ color: tip.accent || undefined }}>
          {tip.title}
        </div>
      )}
      {(tip.lines || []).map((l, i) => (
        <div key={i} className="chart-tooltip-row">
          <span className="chart-tooltip-key" style={{ flexShrink: 0 }}>{l[0]}</span>
          <span className="chart-tooltip-val" style={{
            color: l[2] || undefined,
            wordBreak: 'break-all', whiteSpace: 'normal', textAlign: 'right',
          }}>{l[1]}</span>
        </div>
      ))}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Response Sizes panel — visible-text-character daily-bucketed time
// series per model. Each line = that model's daily median chars in
// `text` content blocks; dashed line = p90. Log y-axis (response
// sizes span 4+ orders of magnitude). Chars (not output_tokens)
// because output_tokens silently includes thinking, and per-model
// thinking shares vary 0.7%–25% — token-based percentiles would
// conflate "longer responses" with "more thinking".
// ──────────────────────────────────────────────────────────────────────
function ResponseSizesPanel({ data, bucketS }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  // Build per-model series (sorted by ts ascending). Drop <synthetic>.
  const series = React.useMemo(() => {
    const drop = (k) => k === '<synthetic>' || k === 'synthetic';
    const out = new Map();
    for (const d of data || []) {
      if (!(d.n > 0)) continue;
      const key = shortModelName(d.model);
      if (drop(key)) continue;
      const ts = Date.parse(d.ts);
      if (isNaN(ts)) continue;
      if (!out.has(key)) out.set(key, []);
      out.get(key).push({ ts, n: d.n, p50: d.p50, p90: d.p90 });
    }
    const result = [];
    const halfMs = ((bucketS || 86400) * 1000) / 2;
    for (const [key, points] of out) {
      points.sort((a, b) => a.ts - b.ts);
      const n = points.reduce((s, p) => s + p.n, 0);
      const extended = extendBucketSeries(
        points, halfMs, ['p50', 'p90'], { log: true, min: 0 }
      );
      result.push({ key, points: extended, n });
    }
    result.sort((a, b) => b.n - a.n);
    return result;
  }, [data, bucketS]);

  // All models on by default, user can toggle any off.
  const [overrides, setOverrides] = React.useState({});
  const sel = React.useMemo(() => {
    const s = new Set(series.map(m => m.key));
    for (const [k, on] of Object.entries(overrides)) {
      if (on) s.add(k); else s.delete(k);
    }
    return s;
  }, [series, overrides]);
  function toggle(k) {
    setOverrides(prev => ({ ...prev, [k]: !sel.has(k) }));
  }
  const visible = series.filter(m => sel.has(m.key));

  // X-domain: union of all visible timestamps. Y-domain: log of p90 max.
  let tMin = Infinity, tMax = -Infinity, yMaxRaw = 1;
  for (const s of visible) {
    for (const p of s.points) {
      if (p.ts < tMin) tMin = p.ts;
      if (p.ts > tMax) tMax = p.ts;
      if (p.p90 > yMaxRaw) yMaxRaw = p.p90;
    }
  }
  if (!isFinite(tMin) || !isFinite(tMax) || tMin === tMax) {
    tMin = Date.now() - 24 * 3600 * 1000;
    tMax = Date.now();
  }
  const yMin = 1;
  const yMax = Math.max(10, yMaxRaw * 1.2);
  const logYMin = Math.log10(yMin);
  const logYMax = Math.log10(yMax);

  const padL = 56, padR = 30, padT = 16, padB = 30;
  const h = 280;
  const plotW = Math.max(20, w - padL - padR);
  const plotH = h - padT - padB;
  const xScale = ts => padL + ((ts - tMin) / Math.max(1, tMax - tMin)) * plotW;
  const yScale = v => padT + plotH - ((Math.log10(Math.max(yMin, v)) - logYMin) / (logYMax - logYMin)) * plotH;

  // Y-axis decade ticks.
  const yTicks = [];
  for (let p = Math.ceil(logYMin); p <= Math.floor(logYMax); p++) yTicks.push(Math.pow(10, p));

  // X-axis: adaptive labels (UTC).
  const xTicks = window.timeTicksUTC(tMin, tMax);

  function onMove(e) {
    // Use the SVG's own bounding rect — the panel wraps the SVG in a
    // nested div, so the outer container ref would offset by header +
    // checkbox row heights and break the hit-test entirely.
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    // Hit-test against the LINES (not just discrete day points) so
    // hovering between two daily buckets snaps to the model's
    // interpolated value at the cursor's x. Linear interp in log-y
    // space matches what's drawn (the polyline between two log-mapped
    // points is a straight line in screen space).
    let best = null, bestD = 1e9, bestKey = null;
    for (const s of visible) {
      const pts = s.points;
      if (!pts.length) continue;
      // Skip this model entirely when the cursor is outside its real
      // data x-range (the polyline only spans first→last point — past
      // those, the line doesn't exist, so we shouldn't hover it).
      const firstX = xScale(pts[0].ts);
      const lastX  = xScale(pts[pts.length - 1].ts);
      if (mx < firstX - 2 || mx > lastX + 2) continue;
      // Find the segment whose x-range contains mx.
      let i = 0;
      while (i < pts.length - 1 && xScale(pts[i + 1].ts) < mx) i++;
      const a = pts[i];
      const b = pts[Math.min(i + 1, pts.length - 1)];
      const ax = xScale(a.ts), bx = xScale(b.ts);
      const t = (a === b || bx === ax) ? 0 : Math.max(0, Math.min(1, (mx - ax) / (bx - ax)));
      const ts   = a.ts  + t * (b.ts  - a.ts);
      const lerpLog = (av, bv) => {
        const la = Math.log10(Math.max(1, av));
        const lb = Math.log10(Math.max(1, bv));
        return Math.pow(10, la + t * (lb - la));
      };
      const p50 = lerpLog(a.p50, b.p50);
      const p90 = lerpLog(a.p90, b.p90);
      const n   = Math.round(a.n + t * (b.n - a.n));
      const py = yScale(p50);
      const d = Math.abs(py - my);  // X is exactly at cursor, so just Y distance
      if (d < bestD) {
        bestD = d;
        bestKey = s.key;
        best = { ts, p50, p90, n };
      }
    }
    if (!best || bestD > 32) { setTip(null); return; }
    const fmt = window.humanFmt;
    setTip({
      x: mx, y: my,
      title: bestKey + ' · ' + new Date(best.ts).toISOString().slice(0, 10),
      accent: (window.modelColors && window.modelColors[bestKey]) || '#888',
      lines: [
        ['responses', best.n.toLocaleString()],
        ['median',    fmt(Math.round(best.p50))],
        ['p90',       fmt(Math.round(best.p90))],
      ],
    });
  }

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}` }}>
        <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
          Response Sizes by Model
        </div>
        <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
          daily median + p90 of visible response characters (text blocks; thinking excluded) · log y-axis · solid = median, dashed = p90
        </div>
      </div>

      <div style={{
        padding: '8px 14px', borderTop: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '6px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
        order: 99,
      }}>
        <span>show:</span>
        {series.map(m => {
          const c = (window.modelColors && window.modelColors[m.key]) || '#888';
          const checked = sel.has(m.key);
          return (
            <label key={m.key} style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              cursor: 'pointer', userSelect: 'none',
              opacity: checked ? 1 : 0.6,
            }}>
              <input type="checkbox" checked={checked} onChange={() => toggle(m.key)}
                style={{ accentColor: c, margin: 0 }} />
              <span style={{ width: 10, height: 10, background: c, display: 'inline-block', borderRadius: 2 }} />
              <span style={{ color: TH_X.text, fontWeight: 600 }}>{m.key}</span>
              <span style={{ color: TH_X.textDim }}>({m.n.toLocaleString()})</span>
            </label>
          );
        })}
        {!series.length && <span>no responses in range</span>}
      </div>

      <div style={{ position: 'relative' }} onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
        <svg width={w} height={h} style={{ display: 'block' }}>
          {/* Y grid */}
          {yTicks.map((v, i) => (
            <line key={'g'+i} x1={padL} x2={w - padR}
              y1={yScale(v)} y2={yScale(v)}
              stroke={TH_X.grid} strokeOpacity="0.25" />
          ))}

          {/* Lines per visible model — p90 dashed underneath, median on top */}
          {visible.map(s => {
            const c = (window.modelColors && window.modelColors[s.key]) || '#888';
            const ptsP90 = s.points
              .filter(p => p.p90 > 0)
              .map(p => `${xScale(p.ts)},${yScale(p.p90)}`).join(' ');
            return (
              <polyline key={'p90-'+s.key} points={ptsP90}
                stroke={c} strokeWidth="1.1" strokeDasharray="4,3"
                strokeOpacity="0.7" fill="none" />
            );
          })}
          {visible.map(s => {
            const c = (window.modelColors && window.modelColors[s.key]) || '#888';
            const ptsP50 = s.points
              .filter(p => p.p50 > 0)
              .map(p => `${xScale(p.ts)},${yScale(p.p50)}`).join(' ');
            return (
              <polyline key={'p50-'+s.key} points={ptsP50}
                stroke={c} strokeWidth="1.8" fill="none" />
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
              {window.humanFmt(v)}
            </text>
          ))}
          {/* X labels */}
          {xTicks.map((t, i) => (
            <text key={'xl'+i} x={xScale(t.ts)} y={h - padB + 14}
              fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
              {t.label}
            </text>
          ))}
          <text x={14} y={padT + plotH/2} fontSize="9" fill={TH_X.textDim}
            textAnchor="middle" fontFamily="monospace"
            transform={`rotate(-90 14 ${padT + plotH/2})`}>visible chars (log)</text>
        </svg>
        {tip && <window.DashTooltip tip={tip} />}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Tool Usage panel — daily-bucketed share-of-total tool-call ratios
// per tool, stacked to 100%. A tool is promoted to its own band if
// it ever cracked top-N at any single bucket (so a newcomer that
// ramped recently gets visibility, not buried in "Other"). User can
// override per-tool via checkboxes. Hovering "Other" shows the full
// per-bin breakdown of the unpromoted tools.
// ──────────────────────────────────────────────────────────────────────

// Stable color picker — hash a tool name to a hue. Avoids manually
// curating a palette for ~80 tools while keeping each tool's color
// stable across reloads and panels.
function _toolColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  const hue = ((h % 360) + 360) % 360;
  return `hsl(${hue}, 60%, 55%)`;
}
const _OTHER_COLOR = '#5a627a';

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

  // Two cells + 16px row padding + 8px gap + 2px border per cell.
  const cellW = Math.max(280, (w - 16 - 8 - 4) / 2);
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
              bucketMs={bucketMs} />
          ))}
          {row.length === 1 && <div style={{ width: cellW }} />}
        </div>
      ))}
    </div>
  );
}

function ToolErrorSubPanel({ modelName, modelData, w, h, bucketMs }) {
  const AGGREGATE = '__AGG__';
  const OTHER = '__OTHER__';
  // Cap visible per-tool checkboxes; rest collapse into a single
  // "Other" series. Mirrors ToolUsagePanel's TOP_N treatment so the
  // checkbox row stays readable on models with many tools.
  const TOP_N = 5;

  // Tools sorted by total count, split into visible top-N and Other.
  const sortedTools = React.useMemo(() =>
    [...modelData.totalsByTool.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(e => e[0])
  , [modelData]);

  const visibleTools = React.useMemo(
    () => sortedTools.slice(0, TOP_N),
    [sortedTools]
  );
  const otherTools = React.useMemo(
    () => sortedTools.slice(TOP_N),
    [sortedTools]
  );
  const hasOther = otherTools.length > 0;

  // Top-3 of the visible tools default ON alongside AGGREGATE.
  const topTools = React.useMemo(
    () => visibleTools.slice(0, 3),
    [visibleTools]
  );

  // Default ON: AGGREGATE + top-3 visible tools + OTHER (when present).
  // User overrides layered on top.
  const [overrides, setOverrides] = React.useState({});
  const sel = React.useMemo(() => {
    const s = new Set([AGGREGATE, ...topTools]);
    if (hasOther) s.add(OTHER);
    for (const [k, on] of Object.entries(overrides)) {
      if (on) s.add(k); else s.delete(k);
    }
    return s;
  }, [topTools, hasOther, overrides]);

  function toggle(k) {
    setOverrides(prev => ({ ...prev, [k]: !sel.has(k) }));
  }

  // Build per-series rate sequences. Each series: array of
  // { t_ms, rate } at non-sparse buckets only (n_total > 0).
  // Aggregate covers all tools; visible tools are individual; Other
  // is the bucket-wise sum across `otherTools`.
  const series = React.useMemo(() => {
    const out = new Map();
    out.set(AGGREGATE, []);
    for (const tool of visibleTools) out.set(tool, []);
    if (hasOther) out.set(OTHER, []);
    for (const ts of modelData.buckets) {
      const m = modelData.perBucketTool.get(ts);
      // Aggregate
      let aT = 0, aE = 0;
      for (const v of m.values()) { aT += v.n_total; aE += v.n_error; }
      if (aT > 0) out.get(AGGREGATE).push({ t_ms: ts, rate: aE / aT, n_total: aT, n_error: aE });
      // Visible per-tool
      for (const tool of visibleTools) {
        const v = m.get(tool);
        if (v && v.n_total > 0) {
          out.get(tool).push({ t_ms: ts, rate: v.n_error / v.n_total, n_total: v.n_total, n_error: v.n_error });
        }
      }
      // Other (sum across non-visible tools)
      if (hasOther) {
        let oT = 0, oE = 0;
        for (const tool of otherTools) {
          const v = m.get(tool);
          if (v) { oT += v.n_total; oE += v.n_error; }
        }
        if (oT > 0) out.get(OTHER).push({ t_ms: ts, rate: oE / oT, n_total: oT, n_error: oE });
      }
    }
    return out;
  }, [modelData, visibleTools, otherTools, hasOther]);

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
    if (key === OTHER) return _OTHER_COLOR;
    return _toolColor(key);
  }

  function labelFor(key) {
    if (key === AGGREGATE) return 'Aggregate';
    if (key === OTHER) return `Other (${otherTools.length})`;
    return key;
  }

  const [tip, setTip] = React.useState(null);

  function onMove(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    if (!modelData.buckets.length) { setTip(null); return; }
    // Snap to nearest bucket center on x.
    let bIdx = 0, bestD = 1e9;
    for (let i = 0; i < modelData.buckets.length; i++) {
      const cx = xs(modelData.buckets[i] + bucketMs / 2);
      const d = Math.abs(cx - mx);
      if (d < bestD) { bestD = d; bIdx = i; }
    }
    const ts = modelData.buckets[bIdx];
    const m = modelData.perBucketTool.get(ts);
    let aT = 0, aE = 0;
    for (const v of m.values()) { aT += v.n_total; aE += v.n_error; }
    const lines = [];
    lines.push(['aggregate', aT ? `${aE}/${aT} = ${((aE / aT) * 100).toFixed(2)}%` : '-']);
    for (const k of [...sel].filter(k => k !== AGGREGATE)) {
      if (k === OTHER) {
        let oT = 0, oE = 0;
        for (const tool of otherTools) {
          const v = m.get(tool);
          if (v) { oT += v.n_total; oE += v.n_error; }
        }
        if (oT > 0) lines.push([`other (${otherTools.length})`,
          `${oE}/${oT} = ${((oE / oT) * 100).toFixed(2)}%`]);
      } else {
        const v = m.get(k);
        if (v) lines.push([k, `${v.n_error}/${v.n_total} = ${((v.n_error / v.n_total) * 100).toFixed(2)}%`]);
      }
    }
    setTip({
      x: mx, y: my,
      title: new Date(ts).toISOString().replace('T', ' ').slice(0, 16) + ' UTC',
      accent: '#ddd',
      lines,
    });
  }

  return (
    <div style={{
      width: w, border: `1px solid ${TH_X.border}`, borderRadius: 3,
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '6px 10px', fontFamily: 'monospace', fontSize: 11,
                    color: TH_X.text, fontWeight: 700, borderBottom: `1px solid ${TH_X.border}` }}>
        {modelName}
      </div>

      <div style={{ position: 'relative' }} onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
        <svg width={w} height={h} style={{ display: 'block' }}>
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

          {tip && (
            <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
              stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
          )}
        </svg>
        {tip && <window.DashTooltip tip={tip} />}
      </div>

      {/* Checkbox row (below the SVG, matches ToolUsagePanel's
          order: 99 / borderTop layout). */}
      <div style={{
        padding: '8px 14px', borderTop: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '6px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
      }}>
        <span>show:</span>
        {[AGGREGATE, ...visibleTools, ...(hasOther ? [OTHER] : [])].map(k => {
          const c = colorFor(k);
          const checked = sel.has(k);
          const totalForKey = k === AGGREGATE
            ? [...modelData.totalsByTool.values()].reduce((s, n) => s + n, 0)
            : k === OTHER
              ? otherTools.reduce((s, t) => s + (modelData.totalsByTool.get(t) || 0), 0)
              : (modelData.totalsByTool.get(k) || 0);
          return (
            <label key={k} style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              cursor: 'pointer', userSelect: 'none',
              opacity: checked ? 1 : 0.6,
            }}>
              <input type="checkbox" checked={checked} onChange={() => toggle(k)}
                style={{ accentColor: c, margin: 0 }} />
              <span style={{ width: 10, height: 10, background: c, display: 'inline-block', borderRadius: 2 }} />
              <span style={{ color: TH_X.text, fontWeight: 600 }}>{labelFor(k)}</span>
              <span style={{ color: TH_X.textDim }}>({totalForKey.toLocaleString()})</span>
            </label>
          );
        })}
      </div>
    </div>
  );
}

function ToolUsagePanel({ models, project, range, nonce }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  const [data, setData] = React.useState([]);
  const [bucketMs, setBucketMs] = React.useState(86_400_000);
  // Per-panel model filter — separate from any global picker so the
  // user can drill into "what does opus-4-7 use Bash for?" without
  // affecting other panels.
  const [activeModel, setActiveModel] = React.useState('');

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  React.useEffect(() => {
    const q = (project ? `&project=${encodeURIComponent(project)}` : '')
            + (activeModel ? `&model=${encodeURIComponent(activeModel)}` : '');
    fetch(`/api/tool-usage?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => {
        setData(b.buckets || []);
        if (b.bucket_s) setBucketMs(b.bucket_s * 1000);
      })
      .catch(err => console.error('tool-usage fetch failed', err));
  }, [project, range, activeModel, nonce]);

  // Dedup model list by short name for the select.
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

  const TOP_N = 7;

  // 1) Pivot data into a Map<bucketTs, Map<tool, count>> + bucket totals.
  // 2) Compute per-tool overall count.
  // 3) Determine "promoted" tools: top-N at any single bucket.
  const { buckets, perBucket, totalsByTool, promoted } = React.useMemo(() => {
    const perBucket = new Map();      // ts -> Map<tool, n>
    const totalsByTool = new Map();   // tool -> total n across all buckets
    for (const r of data || []) {
      const t = Date.parse(r.ts);
      if (isNaN(t)) continue;
      if (!perBucket.has(t)) perBucket.set(t, new Map());
      const cur = perBucket.get(t).get(r.tool) || 0;
      perBucket.get(t).set(r.tool, cur + r.n);
      totalsByTool.set(r.tool, (totalsByTool.get(r.tool) || 0) + r.n);
    }
    const buckets = [...perBucket.keys()].sort((a, b) => a - b);
    // Per-bucket top-N → union → promoted set.
    const promoted = new Set();
    for (const ts of buckets) {
      const entries = [...perBucket.get(ts).entries()].sort((a, b) => b[1] - a[1]);
      for (const [tool] of entries.slice(0, TOP_N)) promoted.add(tool);
    }
    return { buckets, perBucket, totalsByTool, promoted };
  }, [data]);

  // Sorted promoted-tool list (largest overall first → big bands at bottom).
  const promotedList = React.useMemo(
    () => [...promoted].sort((a, b) => (totalsByTool.get(b) || 0) - (totalsByTool.get(a) || 0)),
    [promoted, totalsByTool]
  );
  const otherTools = React.useMemo(
    () => [...totalsByTool.keys()]
      .filter(t => !promoted.has(t))
      .sort((a, b) => (totalsByTool.get(b) || 0) - (totalsByTool.get(a) || 0)),
    [totalsByTool, promoted]
  );

  // Per-tool checkbox overrides — start with all promoted shown.
  // Treat the literal key "__OTHER__" the same way so the user can
  // toggle the Other band off when it's not interesting.
  const [overrides, setOverrides] = React.useState({});
  const sel = React.useMemo(() => {
    const s = new Set(promotedList);
    s.add('__OTHER__');
    for (const [k, on] of Object.entries(overrides)) {
      if (on) s.add(k); else s.delete(k);
    }
    return s;
  }, [promotedList, overrides]);
  function toggle(k) {
    setOverrides(prev => ({ ...prev, [k]: !sel.has(k) }));
  }
  // Bands actually drawn (in stacking order, largest at bottom).
  const bands = [...sel].filter(k => k !== '__OTHER__')
    .sort((a, b) => (totalsByTool.get(b) || 0) - (totalsByTool.get(a) || 0));
  const showOther = otherTools.length > 0 && sel.has('__OTHER__');

  // Build per-bucket share series. Each bucket's *displayed* bands +
  // optional Other rescale to sum to 1.0 — so unchecking a tool
  // redistributes the remaining bands across the full 0-100% height
  // (relative ratios among what's shown), not just leaves a hole.
  // Tooltip still has access to the absolute bucket total.
  const grid = React.useMemo(() => {
    const shares = new Map();
    bands.forEach(t => shares.set(t, []));
    const other = [];
    const totalCalls = [];
    for (const ts of buckets) {
      const counts = perBucket.get(ts);
      let bucketTotal = 0;
      for (const v of counts.values()) bucketTotal += v;
      totalCalls.push(bucketTotal);
      let bandSum = 0;
      for (const t of bands) bandSum += counts.get(t) || 0;
      let otherSum = 0;
      if (showOther) for (const t of otherTools) otherSum += counts.get(t) || 0;
      const denom = bandSum + (showOther ? otherSum : 0);
      for (const t of bands) {
        const v = counts.get(t) || 0;
        shares.get(t).push(denom > 0 ? v / denom : 0);
      }
      other.push(showOther && denom > 0 ? otherSum / denom : 0);
    }
    // Extend the stacked-area by half a bucket on each end so the
    // visual reaches the bucket edges (no half-bucket gap). Shares
    // and Other are linearly extrapolated from the two adjacent
    // buckets, clamped ≥ 0. Per-band sums at the new boundaries are
    // then renormalized to 1.0 so the stack never overshoots 100%.
    if (buckets.length >= 2 && bucketMs > 0) {
      const halfMs = bucketMs / 2;
      const extrap = (arr) => {
        if (arr.length < 2) return [arr[0] || 0, ...arr, arr[arr.length - 1] || 0];
        const first = Math.max(0, 1.5 * arr[0] - 0.5 * arr[1]);
        const last  = Math.max(0, 1.5 * arr[arr.length - 1] - 0.5 * arr[arr.length - 2]);
        return [first, ...arr, last];
      };
      const newShares = new Map();
      for (const t of bands) newShares.set(t, extrap(shares.get(t)));
      const newOther = extrap(other);
      const newTotal = extrap(totalCalls);
      // Renormalize boundary points so band sum + other = 1 there
      // (independent extrapolation can drift the sum away from 1).
      const fixIdx = (idx) => {
        let sum = 0;
        for (const t of bands) sum += newShares.get(t)[idx];
        if (showOther) sum += newOther[idx];
        if (sum <= 0) return;
        for (const t of bands) newShares.get(t)[idx] = newShares.get(t)[idx] / sum;
        if (showOther) newOther[idx] = newOther[idx] / sum;
      };
      fixIdx(0);
      fixIdx(newOther.length - 1);
      return {
        ts: [buckets[0] - halfMs, ...buckets, buckets[buckets.length - 1] + halfMs],
        shares: newShares,
        other: newOther,
        totalCalls: newTotal,
      };
    }
    return { ts: buckets, shares, other, totalCalls };
  }, [buckets, perBucket, bands, showOther, otherTools, bucketMs]);

  // Geometry
  const padL = 56, padR = 30, padT = 16, padB = 30;
  const h = 320;
  const plotW = Math.max(20, w - padL - padR);
  const plotH = h - padT - padB;
  const tMin = grid.ts[0] || (Date.now() - 24 * 3600 * 1000);
  const tMax = grid.ts[grid.ts.length - 1] || Date.now();
  const xScale = ts => padL + ((ts - tMin) / Math.max(1, tMax - tMin)) * plotW;
  const yScale = frac => padT + plotH - frac * plotH;

  // Buckets where the displayed bands+Other sum to 0 carry no data
  // FOR THE CURRENT FILTER — interpolate across them instead of
  // collapsing to baseline (which reads as a hard "data ends here").
  const liveIdx = React.useMemo(() => {
    const out = [];
    for (let i = 0; i < grid.ts.length; i++) {
      let total = showOther ? grid.other[i] : 0;
      for (const t of bands) total += grid.shares.get(t)[i];
      if (total > 0) out.push(i);
    }
    return out;
  }, [grid, bands, showOther]);

  // Stacked-area paths. Bottom-up: largest band first, "Other" last.
  // Path walks `liveIdx` only — gaps are bridged by linear segments
  // between adjacent live buckets.
  const stackPaths = React.useMemo(() => {
    const out = [];
    if (!liveIdx.length) return out;
    const cum = new Array(grid.ts.length).fill(0);
    const layers = [...bands.map(t => ({ tool: t, color: _toolColor(t), shares: grid.shares.get(t) }))];
    if (showOther) layers.push({ tool: '__OTHER__', color: _OTHER_COLOR, shares: grid.other });
    for (const layer of layers) {
      const top = [], bot = [];
      for (const i of liveIdx) {
        const baseY = yScale(cum[i]);
        const topY  = yScale(cum[i] + layer.shares[i]);
        bot.push(`${xScale(grid.ts[i])},${baseY}`);
        top.push(`${xScale(grid.ts[i])},${topY}`);
        cum[i] += layer.shares[i];
      }
      const d = `M ${top.join(' L ')} L ${bot.reverse().join(' L ')} Z`;
      out.push({ tool: layer.tool, color: layer.color, d });
    }
    return out;
  }, [grid, bands, showOther, liveIdx, plotW, plotH, tMin, tMax]);

  // Y-axis ticks at 0/25/50/75/100%.
  const yTicks = [0, 0.25, 0.5, 0.75, 1.0];
  // X-axis: adaptive labels.
  const xTicks = (isFinite(tMin) && isFinite(tMax)) ? window.timeTicksUTC(tMin, tMax) : [];

  function onMove(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    if (!grid.ts.length) { setTip(null); return; }
    // Snap to nearest bucket on x.
    let bIdx = 0, bestD = 1e9;
    for (let i = 0; i < grid.ts.length; i++) {
      const d = Math.abs(xScale(grid.ts[i]) - mx);
      if (d < bestD) { bestD = d; bIdx = i; }
    }
    const cursorFrac = 1 - (my - padT) / plotH;
    // Identify which band the cursor is in (bottom-up cumulative).
    let cum = 0, hovered = null;
    for (const t of bands) {
      const sh = grid.shares.get(t)[bIdx];
      if (cursorFrac >= cum && cursorFrac < cum + sh) { hovered = t; break; }
      cum += sh;
    }
    if (!hovered && showOther && cursorFrac >= cum && cursorFrac < cum + grid.other[bIdx]) {
      hovered = '__OTHER__';
    }
    if (!hovered) { setTip(null); return; }
    const ts = grid.ts[bIdx];
    const totalCalls = grid.totalCalls[bIdx];
    const dateStr = new Date(ts).toISOString().slice(0, 10);
    const lines = [];
    // Denom = sum across what's actually displayed in this bucket
    // (matches the chart's rescaled share %).
    const counts = perBucket.get(ts);
    let bandSum = 0;
    for (const t of bands) bandSum += counts.get(t) || 0;
    let otherSum = 0;
    if (showOther) for (const t of otherTools) otherSum += counts.get(t) || 0;
    const shownDenom = Math.max(1, bandSum + (showOther ? otherSum : 0));
    if (hovered === '__OTHER__') {
      const otherEntries = otherTools
        .map(t => ({ tool: t, n: counts.get(t) || 0 }))
        .filter(e => e.n > 0)
        .sort((a, b) => b.n - a.n);
      const otherTotal = otherEntries.reduce((s, e) => s + e.n, 0);
      lines.push(['Other share',  (otherTotal / shownDenom * 100).toFixed(1) + '% of shown']);
      lines.push(['Other calls',  otherTotal.toLocaleString() + ' (bucket total: ' + totalCalls.toLocaleString() + ')']);
      for (const e of otherEntries) {
        lines.push([
          e.tool,
          `${(e.n / shownDenom * 100).toFixed(1)}% (${e.n.toLocaleString()})`,
        ]);
      }
      setTip({
        x: mx, y: my,
        title: `Other · ${dateStr}`,
        accent: _OTHER_COLOR,
        lines,
      });
    } else {
      const n = counts.get(hovered) || 0;
      setTip({
        x: mx, y: my,
        title: `${hovered} · ${dateStr}`,
        accent: _toolColor(hovered),
        lines: [
          ['share',     (n / shownDenom * 100).toFixed(1) + '% of shown'],
          ['absolute',  (n / Math.max(1, totalCalls) * 100).toFixed(1) + '% of bucket'],
          ['calls',     n.toLocaleString() + ' / ' + totalCalls.toLocaleString()],
        ],
      });
    }
  }

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, display: 'flex', alignItems: 'center', gap: 16 }}>
        <div style={{ flex: 1 }}>
          <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
            Tool Usage Ratio over Time
          </div>
          <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
            stacked share of tool calls per day · top-{TOP_N}-at-any-bucket promoted to own band · {showOther ? `${otherTools.length} smaller tools collapsed into Other (hover to expand)` : 'no Other bucket'}
          </div>
        </div>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim }}>
          <button
            type="button"
            onClick={() => {
              const next = {};
              for (const t of promotedList) next[t] = false;
              next['__OTHER__'] = false;
              setOverrides(next);
            }}
            style={{
              background: 'transparent', color: TH_X.textDim,
              border: `1px solid ${TH_X.border}`, borderRadius: 3,
              padding: '2px 8px', fontFamily: 'monospace', fontSize: 11,
              cursor: 'pointer',
            }}
          >none</button>
          <button
            type="button"
            onClick={() => {
              const next = {};
              for (const t of promotedList) next[t] = true;
              next['__OTHER__'] = true;
              setOverrides(next);
            }}
            style={{
              background: 'transparent', color: TH_X.textDim,
              border: `1px solid ${TH_X.border}`, borderRadius: 3,
              padding: '2px 8px', fontFamily: 'monospace', fontSize: 11,
              cursor: 'pointer',
            }}
          >all</button>
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

      <div style={{
        padding: '8px 14px', borderTop: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '6px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
        order: 99,
      }}>
        <span>show:</span>
        {promotedList.map(tool => {
          const c = _toolColor(tool);
          const checked = sel.has(tool);
          return (
            <label key={tool} style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              cursor: 'pointer', userSelect: 'none',
              opacity: checked ? 1 : 0.6,
            }}>
              <input type="checkbox" checked={checked} onChange={() => toggle(tool)}
                style={{ accentColor: c, margin: 0 }} />
              <span style={{ width: 10, height: 10, background: c, display: 'inline-block', borderRadius: 2 }} />
              <span style={{ color: TH_X.text, fontWeight: 600 }}>{tool}</span>
              <span style={{ color: TH_X.textDim }}>({(totalsByTool.get(tool) || 0).toLocaleString()})</span>
            </label>
          );
        })}
        {otherTools.length > 0 && (() => {
          const checked = sel.has('__OTHER__');
          return (
            <label style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              cursor: 'pointer', userSelect: 'none',
              opacity: checked ? 1 : 0.6,
            }}>
              <input type="checkbox" checked={checked} onChange={() => toggle('__OTHER__')}
                style={{ accentColor: _OTHER_COLOR, margin: 0 }} />
              <span style={{ width: 10, height: 10, background: _OTHER_COLOR, display: 'inline-block', borderRadius: 2 }} />
              <span style={{ color: TH_X.text, fontWeight: 600 }}>Other</span>
              <span style={{ color: TH_X.textDim }}>({otherTools.length} tools)</span>
            </label>
          );
        })()}
        {!promotedList.length && <span>no tool data in range</span>}
      </div>

      <div style={{ position: 'relative' }} onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
        <svg width={w} height={h} style={{ display: 'block' }}>
          {/* Y grid */}
          {yTicks.map((v, i) => (
            <line key={'g'+i} x1={padL} x2={w - padR}
              y1={yScale(v)} y2={yScale(v)}
              stroke={TH_X.grid} strokeOpacity="0.25" />
          ))}

          {/* Stacked bands */}
          {stackPaths.map(layer => (
            <path key={layer.tool} d={layer.d}
              fill={layer.color} fillOpacity="0.85" stroke="none" />
          ))}

          {/* Y labels */}
          {yTicks.map((v, i) => (
            <text key={'yl'+i} x={padL - 6} y={yScale(v) + 3}
              fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              {(v * 100).toFixed(0)}%
            </text>
          ))}
          {/* X labels */}
          {xTicks.map((t, i) => (
            <text key={'xl'+i} x={xScale(t.ts)} y={h - padB + 14}
              fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
              {t.label}
            </text>
          ))}
          {tip && (
            <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
              stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
          )}
        </svg>
        {tip && <window.DashTooltip tip={tip} />}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Reply Latency panel — per-(bucket, model) p10–p90 band + median line,
// plus scatter dots for top-1% slowest + bottom-1% fastest replies
// per bucket (when bucket_n >= 100). Log y-axis (latency 0.5s–400s).
// ──────────────────────────────────────────────────────────────────────
function ReplyLatencyPanel({ project, range, nonce, models }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(1200);
  const [tip, setTip] = React.useState(null);
  const [bands, setBands] = React.useState([]);
  const [outliers, setOutliers] = React.useState([]);
  const [bucketMs, setBucketMs] = React.useState(86_400_000);
  const [activeModel, setActiveModel] = React.useState('');

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  React.useEffect(() => {
    const q = (project ? `&project=${encodeURIComponent(project)}` : '')
            + (activeModel ? `&model=${encodeURIComponent(activeModel)}` : '');
    fetch(`/api/reply-latency?range=${range || 'all'}${q}`, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(b => {
        setBands(b.bands || []);
        setOutliers(b.outliers || []);
        if (b.bucket_s) setBucketMs(b.bucket_s * 1000);
      })
      .catch(err => console.error('reply-latency fetch failed', err));
  }, [project, range, activeModel, nonce]);

  // Per-model series for the bands.
  const series = React.useMemo(() => {
    const drop = k => k === '<synthetic>' || k === 'synthetic';
    const out = new Map();
    for (const b of bands) {
      const key = shortModelName(b.model);
      if (drop(key)) continue;
      const ts = Date.parse(b.ts);
      if (isNaN(ts)) continue;
      if (!out.has(key)) out.set(key, []);
      out.get(key).push({ ts, n: b.n, p10: b.p10, p50: b.p50, p90: b.p90 });
    }
    const arr = [];
    const half = bucketMs / 2;
    for (const [key, points] of out) {
      points.sort((a, b) => a.ts - b.ts);
      const n = points.reduce((s, p) => s + p.n, 0);
      // Log-space linear extrapolation by half a bucket on each end
      // so the median + p10/p90 band visually span the full bucket
      // extent without flat-carry artifacts.
      const extended = extendBucketSeries(
        points, half, ['p10', 'p50', 'p90'], { log: true, min: 0 }
      );
      arr.push({ key, points: extended, n });
    }
    arr.sort((a, b) => b.n - a.n);
    return arr;
  }, [bands, bucketMs]);

  // All models on by default; uncheck individually.
  const [overrides, setOverrides] = React.useState({});
  const sel = React.useMemo(() => {
    const s = new Set(series.map(m => m.key));
    for (const [k, on] of Object.entries(overrides)) {
      if (on) s.add(k); else s.delete(k);
    }
    return s;
  }, [series, overrides]);
  function toggle(k) {
    setOverrides(prev => ({ ...prev, [k]: !sel.has(k) }));
  }
  const visible = series.filter(m => sel.has(m.key));

  // Outlier dots filtered by visible models too.
  const visibleKeys = React.useMemo(() => new Set(visible.map(s => s.key)), [visible]);
  const visibleOutliers = React.useMemo(
    () => outliers
      .map(o => ({ ...o, key: shortModelName(o.model), tsMs: Date.parse(o.ts) }))
      .filter(o => !isNaN(o.tsMs) && visibleKeys.has(o.key)),
    [outliers, visibleKeys]
  );

  // Dedup model list for the model select.
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

  // Geometry. Y log-scale, range from 0.1s to max p90 (clamped >= 10s).
  let tMin = Infinity, tMax = -Infinity, yMaxRaw = 1;
  for (const s of visible) {
    for (const p of s.points) {
      if (p.ts < tMin) tMin = p.ts;
      if (p.ts > tMax) tMax = p.ts;
      if (p.p90 > yMaxRaw) yMaxRaw = p.p90;
    }
  }
  for (const o of visibleOutliers) {
    if (o.tsMs < tMin) tMin = o.tsMs;
    if (o.tsMs > tMax) tMax = o.tsMs;
    if (o.latency_s > yMaxRaw) yMaxRaw = o.latency_s;
  }
  if (!isFinite(tMin) || !isFinite(tMax) || tMin === tMax) {
    tMin = Date.now() - 24 * 3600 * 1000;
    tMax = Date.now();
  }
  const yMin = 0.1;
  const yMax = Math.max(10, yMaxRaw * 1.2);
  const logYMin = Math.log10(yMin);
  const logYMax = Math.log10(yMax);

  const padL = 56, padR = 30, padT = 16, padB = 30;
  const h = 320;
  const plotW = Math.max(20, w - padL - padR);
  const plotH = h - padT - padB;
  const xScale = ts => padL + ((ts - tMin) / Math.max(1, tMax - tMin)) * plotW;
  const yScale = v => padT + plotH - ((Math.log10(Math.max(yMin, v)) - logYMin) / (logYMax - logYMin)) * plotH;

  // Y decade ticks.
  const yTicks = [];
  for (let p = Math.ceil(logYMin); p <= Math.floor(logYMax); p++) yTicks.push(Math.pow(10, p));

  // X adaptive labels.
  const xTicks = (isFinite(tMin) && isFinite(tMax)) ? window.timeTicksUTC(tMin, tMax) : [];

  function fmtSecs(s) {
    if (s < 1) return s.toFixed(2) + 's';
    if (s < 60) return s.toFixed(1) + 's';
    if (s < 3600) return (s / 60).toFixed(1) + 'm';
    return (s / 3600).toFixed(1) + 'h';
  }

  function onMove(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    if (mx < padL || mx > w - padR || my < padT || my > padT + plotH) {
      setTip(null); return;
    }
    // Try outlier dots first (small targets, but exact times).
    let bestO = null, bestOD = 1e9;
    for (const o of visibleOutliers) {
      const px = xScale(o.tsMs), py = yScale(o.latency_s);
      const d = Math.hypot(px - mx, py - my);
      if (d < bestOD) { bestOD = d; bestO = o; }
    }
    if (bestO && bestOD < 8) {
      // file_key shape: <project>/<session_id>/<filename>.jsonl —
      // drop the project segment, keep session/filename so the
      // tooltip stays narrow but still uniquely identifies the line.
      const fk = String(bestO.file_key || '');
      const fileShort = fk.split('/').slice(-2).join('/');
      setTip({
        x: mx, y: my,
        title: 'outlier · ' + bestO.key,
        accent: (window.modelColors && window.modelColors[bestO.key]) || '#888',
        lines: [
          ['latency', fmtSecs(bestO.latency_s)],
          ['when',    new Date(bestO.tsMs).toISOString().slice(0, 19) + 'Z'],
          ['file',    fileShort],
          ['line',    String(bestO.line || '')],
        ],
      });
      return;
    }
    // Else: nearest median line (interpolated).
    let best = null, bestD = 1e9, bestKey = null;
    for (const s of visible) {
      const pts = s.points;
      if (!pts.length) continue;
      const firstX = xScale(pts[0].ts);
      const lastX  = xScale(pts[pts.length - 1].ts);
      if (mx < firstX - 2 || mx > lastX + 2) continue;
      let i = 0;
      while (i < pts.length - 1 && xScale(pts[i + 1].ts) < mx) i++;
      const a = pts[i];
      const b = pts[Math.min(i + 1, pts.length - 1)];
      const ax = xScale(a.ts), bx = xScale(b.ts);
      const t = (a === b || bx === ax) ? 0 : Math.max(0, Math.min(1, (mx - ax) / (bx - ax)));
      const ts = a.ts + t * (b.ts - a.ts);
      const lerpLog = (av, bv) => {
        const la = Math.log10(Math.max(yMin, av));
        const lb = Math.log10(Math.max(yMin, bv));
        return Math.pow(10, la + t * (lb - la));
      };
      const p10 = lerpLog(a.p10, b.p10);
      const p50 = lerpLog(a.p50, b.p50);
      const p90 = lerpLog(a.p90, b.p90);
      const n   = Math.round(a.n + t * (b.n - a.n));
      const py = yScale(p50);
      const d = Math.abs(py - my);
      if (d < bestD) {
        bestD = d; bestKey = s.key; best = { ts, p10, p50, p90, n };
      }
    }
    if (!best || bestD > 32) { setTip(null); return; }
    setTip({
      x: mx, y: my,
      title: bestKey + ' · ' + new Date(best.ts).toISOString().slice(0, 10),
      accent: (window.modelColors && window.modelColors[bestKey]) || '#888',
      lines: [
        ['replies', best.n.toLocaleString()],
        ['p10',     fmtSecs(best.p10)],
        ['median',  fmtSecs(best.p50)],
        ['p90',     fmtSecs(best.p90)],
      ],
    });
  }

  return (
    <div ref={ref} style={{
      background: TH_X.bgAxes, border: `1px solid ${TH_X.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '10px 14px 4px', borderBottom: `1px solid ${TH_X.border}`, display: 'flex', alignItems: 'center', gap: 16 }}>
        <div style={{ flex: 1 }}>
          <div style={{ color: TH_X.text, fontFamily: 'monospace', fontWeight: 700, fontSize: 14 }}>
            Reply Latency over Time
          </div>
          <div style={{ color: TH_X.textDim, fontFamily: 'monospace', fontSize: 10, marginTop: 2 }}>
            user msg → first assistant event · per-(bucket, model) p10–p90 band, median line, top/bottom-1% outlier dots (bucket n ≥ 100) · log y
          </div>
        </div>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim }}>
          model:
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
        </label>
      </div>

      <div style={{
        padding: '8px 14px', borderTop: `1px solid ${TH_X.border}`,
        display: 'flex', flexWrap: 'wrap', gap: '6px 14px',
        fontFamily: 'monospace', fontSize: 11, color: TH_X.textDim,
        order: 99,
      }}>
        <span>show:</span>
        {series.map(m => {
          const c = (window.modelColors && window.modelColors[m.key]) || '#888';
          const checked = sel.has(m.key);
          return (
            <label key={m.key} style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              cursor: 'pointer', userSelect: 'none',
              opacity: checked ? 1 : 0.6,
            }}>
              <input type="checkbox" checked={checked} onChange={() => toggle(m.key)}
                style={{ accentColor: c, margin: 0 }} />
              <span style={{ width: 10, height: 10, background: c, display: 'inline-block', borderRadius: 2 }} />
              <span style={{ color: TH_X.text, fontWeight: 600 }}>{m.key}</span>
              <span style={{ color: TH_X.textDim }}>({m.n.toLocaleString()})</span>
            </label>
          );
        })}
        {!series.length && <span>no reply-latency data in range</span>}
      </div>

      <div style={{ position: 'relative' }} onMouseMove={onMove} onMouseLeave={() => setTip(null)}>
        <svg width={w} height={h} style={{ display: 'block' }}>
          {yTicks.map((v, i) => (
            <line key={'g'+i} x1={padL} x2={w - padR}
              y1={yScale(v)} y2={yScale(v)}
              stroke={TH_X.grid} strokeOpacity="0.25" />
          ))}

          {/* p10–p90 band per visible model */}
          {visible.map(s => {
            const c = (window.modelColors && window.modelColors[s.key]) || '#888';
            const top = [], bot = [];
            for (const p of s.points) {
              if (!p.p90 || !p.p10) continue;
              top.push(`${xScale(p.ts)},${yScale(p.p90)}`);
              bot.push(`${xScale(p.ts)},${yScale(p.p10)}`);
            }
            if (top.length < 2) return null;
            const ribbon = `M ${top.join(' L ')} L ${bot.reverse().join(' L ')} Z`;
            return <path key={'band-'+s.key} d={ribbon} fill={c} fillOpacity="0.20" stroke="none" />;
          })}

          {/* Median lines */}
          {visible.map(s => {
            const c = (window.modelColors && window.modelColors[s.key]) || '#888';
            const pts = s.points
              .filter(p => p.p50 > 0)
              .map(p => `${xScale(p.ts)},${yScale(p.p50)}`).join(' ');
            return <polyline key={'med-'+s.key} points={pts}
              stroke={c} strokeWidth="1.8" fill="none" />;
          })}

          {/* Outlier dots (top/bottom 1%) */}
          {visibleOutliers.map((o, i) => {
            const c = (window.modelColors && window.modelColors[o.key]) || '#888';
            return <circle key={'o'+i} cx={xScale(o.tsMs)} cy={yScale(o.latency_s)}
              r="2.5" fill={c} fillOpacity="0.6" stroke="none" />;
          })}

          {tip && (
            <line x1={tip.x} x2={tip.x} y1={padT} y2={padT + plotH}
              stroke="#fff" strokeOpacity="0.3" strokeDasharray="2,3" />
          )}

          {yTicks.map((v, i) => (
            <text key={'yl'+i} x={padL - 6} y={yScale(v) + 3}
              fontSize="9" fill={TH_X.textDim} textAnchor="end" fontFamily="monospace">
              {v < 1 ? v.toFixed(1) + 's'
               : v < 60 ? Math.round(v) + 's'
               : v < 3600 ? (v/60).toFixed(v < 600 ? 1 : 0).replace(/\.0$/, '') + 'm'
               : (v/3600).toFixed(v < 36000 ? 1 : 0).replace(/\.0$/, '') + 'h'}
            </text>
          ))}
          {xTicks.map((t, i) => (
            <text key={'xl'+i} x={xScale(t.ts)} y={h - padB + 14}
              fontSize="9" fill={TH_X.textDim} textAnchor="middle" fontFamily="monospace">
              {t.label}
            </text>
          ))}
          <text x={14} y={padT + plotH/2} fontSize="9" fill={TH_X.textDim}
            textAnchor="middle" fontFamily="monospace"
            transform={`rotate(-90 14 ${padT + plotH/2})`}>latency (log)</text>
        </svg>
        {tip && <window.DashTooltip tip={tip} />}
      </div>
    </div>
  );
}

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
  const [metric, setMetric] = React.useState('cost_usd');

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

  const { byCell, maxVal, rowTotals, colTotals, grand, rowMax, colMax } = React.useMemo(() => {
    const byCell = new Map();               // dow*100+hour -> cell
    let maxVal = 0;
    const zero = () => ({ requests: 0, output_tokens: 0, cost_usd: 0 });
    const rowTotals = Array.from({ length: 7 }, zero);
    const colTotals = Array.from({ length: 24 }, zero);
    const grand = zero();
    for (const c of cells || []) {
      byCell.set(c.dow * 100 + c.hour, c);
      maxVal = Math.max(maxVal, c[metric] || 0);
      const r = rowTotals[c.dow - 1];
      const col = colTotals[c.hour];
      for (const k of ['requests', 'output_tokens', 'cost_usd']) {
        const v = c[k] || 0;
        r[k] += v; col[k] += v; grand[k] += v;
      }
    }
    const rowMax = Math.max(...rowTotals.map(r => r[metric]), 0);
    const colMax = Math.max(...colTotals.map(c => c[metric]), 0);
    return { byCell, maxVal, rowTotals, colTotals, grand, rowMax, colMax };
  }, [cells, metric]);

  // Geometry — 25 columns × 8 rows (24 hours + Σ, 7 days + Σ), label gutters left + top.
  const padL = 44, padR = 14, padT = 24, padB = 10, gap = 2;
  const SUM_GAP = 8;
  const cellW = Math.max(8, (w - padL - padR - SUM_GAP - 23 * gap) / 25);
  const cellH = Math.min(34, Math.max(18, cellW * 0.8));
  const h = padT + 7 * cellH + 6 * gap + SUM_GAP + cellH + padB;
  const sumColX = padL + 23 * (cellW + gap) + cellW + SUM_GAP;
  const sumRowY = padT + 6 * (cellH + gap) + cellH + SUM_GAP;

  // Sequential single-hue ramp on the dark surface: intensity = opacity
  // of the metric hue; sqrt keeps the heavy-tailed mid-range readable.
  function fillFor(v, scaleMax) {
    if (!v || scaleMax <= 0) return { color: TH_X.bgDark, opacity: 1 };
    const t = Math.sqrt(v / scaleMax);
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

  function onSumColEnter(e, dow) {
    const rect = ref.current.getBoundingClientRect();
    const r = rowTotals[dow - 1];
    const v = r[metric];
    const pct = grand[metric] > 0 ? (v / grand[metric] * 100).toFixed(1) : '0.0';
    setTip({
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      title: `${_HEAT_DOW[dow - 1]} · all hours`,
      accent: mspec.color,
      lines: [
        ['requests',   r.requests.toLocaleString()],
        ['output tok', humanFmt_X(r.output_tokens)],
        ['cost',       window.humanCurrency(r.cost_usd)],
        ['share',      pct + '% of total'],
      ],
    });
  }

  function onSumRowEnter(e, hour) {
    const rect = ref.current.getBoundingClientRect();
    const c = colTotals[hour];
    const v = c[metric];
    const pct = grand[metric] > 0 ? (v / grand[metric] * 100).toFixed(1) : '0.0';
    setTip({
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      title: `${String(hour).padStart(2, '0')}:00–${String((hour + 1) % 24).padStart(2, '0')}:00 · all days`,
      accent: mspec.color,
      lines: [
        ['requests',   c.requests.toLocaleString()],
        ['output tok', humanFmt_X(c.output_tokens)],
        ['cost',       window.humanCurrency(c.cost_usd)],
        ['share',      pct + '% of total'],
      ],
    });
  }

  function onCornerEnter(e) {
    const rect = ref.current.getBoundingClientRect();
    setTip({
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      title: 'Total',
      accent: mspec.color,
      lines: [
        ['requests',   grand.requests.toLocaleString()],
        ['output tok', humanFmt_X(grand.output_tokens)],
        ['cost',       window.humanCurrency(grand.cost_usd)],
      ],
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
        {/* Separator lines in the SUM_GAP bands — margins read as distinct. */}
        <line x1={padL + 23 * (cellW + gap) + cellW + SUM_GAP / 2}
              x2={padL + 23 * (cellW + gap) + cellW + SUM_GAP / 2}
              y1={padT} y2={sumRowY + cellH}
              stroke="#fff" strokeWidth="1" strokeOpacity={0.85} />
        <line x1={padL}
              x2={sumColX + cellW}
              y1={padT + 6 * (cellH + gap) + cellH + SUM_GAP / 2}
              y2={padT + 6 * (cellH + gap) + cellH + SUM_GAP / 2}
              stroke="#fff" strokeWidth="1" strokeOpacity={0.85} />

        {/* hour labels every 3h */}
        {[0, 3, 6, 9, 12, 15, 18, 21].map(hr => (
          <text key={hr} x={cellRect(1, hr).x + cellW / 2} y={padT - 8}
                textAnchor="middle" fill={TH_X.textDim}
                fontFamily="monospace" fontSize="9">{hr}</text>
        ))}
        {/* Σ column header */}
        <text x={sumColX + cellW / 2} y={padT - 8}
              textAnchor="middle" fill={TH_X.textDim}
              fontFamily="monospace" fontSize="9">Σ</text>

        {/* weekday labels */}
        {_HEAT_DOW.map((d, i) => (
          <text key={d} x={padL - 8} y={padT + i * (cellH + gap) + cellH / 2 + 3}
                textAnchor="end" fill={TH_X.textDim}
                fontFamily="monospace" fontSize="9">{d}</text>
        ))}
        {/* Σ row label */}
        <text x={padL - 8} y={sumRowY + cellH / 2 + 3}
              textAnchor="end" fill={TH_X.textDim}
              fontFamily="monospace" fontSize="9">Σ</text>

        {/* cells */}
        {Array.from({ length: 7 }, (_, di) => di + 1).map(dow =>
          Array.from({ length: 24 }, (_, hour) => {
            const { x, y } = cellRect(dow, hour);
            const c = byCell.get(dow * 100 + hour);
            const v = c ? (c[metric] || 0) : 0;
            const f = fillFor(v, maxVal);
            return (
              <rect key={`${dow}-${hour}`} x={x} y={y}
                    width={cellW} height={cellH} rx="2"
                    fill={f.color} fillOpacity={f.opacity}
                    stroke={v > 0 ? 'none' : TH_X.border}
                    strokeWidth={v > 0 ? 0 : 0.5}
                    onMouseMove={e => onCellEnter(e, dow, hour)} />
            );
          })
        )}

        {/* Σ column (per-weekday totals) */}
        {Array.from({ length: 7 }, (_, i) => i + 1).map(dow => {
          const v = rowTotals[dow - 1][metric];
          const f = fillFor(v, rowMax);
          return (
            <rect key={`sumcol-${dow}`} x={sumColX}
                  y={padT + (dow - 1) * (cellH + gap)}
                  width={cellW} height={cellH} rx="2"
                  fill={f.color} fillOpacity={f.opacity}
                  stroke={v > 0 ? 'none' : TH_X.border}
                  strokeWidth={v > 0 ? 0 : 0.5}
                  onMouseMove={e => onSumColEnter(e, dow)} />
          );
        })}

        {/* Σ row (per-hour totals) */}
        {Array.from({ length: 24 }, (_, hour) => {
          const v = colTotals[hour][metric];
          const f = fillFor(v, colMax);
          return (
            <rect key={`sumrow-${hour}`} x={padL + hour * (cellW + gap)}
                  y={sumRowY} width={cellW} height={cellH} rx="2"
                  fill={f.color} fillOpacity={f.opacity}
                  stroke={v > 0 ? 'none' : TH_X.border}
                  strokeWidth={v > 0 ? 0 : 0.5}
                  onMouseMove={e => onSumRowEnter(e, hour)} />
          );
        })}

        {/* Corner grand total (outline only; value in tooltip) */}
        <rect x={sumColX} y={sumRowY} width={cellW} height={cellH} rx="2"
              fill="none" stroke={TH_X.border} strokeWidth="0.5"
              onMouseMove={onCornerEnter} />
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
        <span style={{ marginLeft: 'auto' }}>intensity ∝ √(value / max) · Σ margins scaled independently</span>
      </div>

      {tip && <window.DashTooltip tip={tip} />}
    </div>
  );
}

window.CacheTTLPanel = CacheTTLPanel;
window.ContextGrowthPanel = ContextGrowthPanel;
window.DashTooltip = DashTooltip;
window.shortModelName = shortModelName;
window.capForModel = capForModel;
window.ResponseSizesPanel = ResponseSizesPanel;
window.ToolUsagePanel = ToolUsagePanel;
window.ActivityHeatmapPanel = ActivityHeatmapPanel;
window.ToolErrorRatePanel = ToolErrorRatePanel;
window.ReplyLatencyPanel = ReplyLatencyPanel;
