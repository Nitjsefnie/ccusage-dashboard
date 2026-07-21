// Dashboard chart components — interactive SVG (with hover tooltips).
// Six time-series cards, two horizontal bars, and the burn-rate panel.

// CSS-var resolver so the chart theme follows the stylesheet.
// Evaluated once at module-load — sufficient for a single-theme app.
const cssVar = (name, fallback) => {
  if (typeof window === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
};

const TH = {
  bgDark:  cssVar('--bg',       '#0a0b10'),
  bgAxes:  cssVar('--bg-card',  '#12141d'),
  border:  cssVar('--border',   '#1f2230'),
  text:    cssVar('--fg',       '#e7e9f2'),
  textDim: cssVar('--muted',    '#6b7193'),
  grid:    'rgba(255,255,255,0.05)',
};

// Series colors — kept as literals so the chart palette is tunable
// independently of the surface tokens.
const COL = {
  inputTokens:       cssVar('--accent', '#00d4aa'),
  outputTokens:      '#ff9c5a',
  cacheCreateTokens: 'oklch(0.72 0.14 305)',
  cacheReadTokens:   'oklch(0.72 0.14 25)',
  totalTokens:       'oklch(0.78 0.14 245)',
  costUSD:           cssVar('--gold', 'oklch(0.85 0.14 90)'),
};

const MODEL_COLORS = {
  'fable-5':     'oklch(0.78 0.17 330)',  // magenta — top tier
  'opus-4-8':    'oklch(0.70 0.17 5)',    // rose
  'opus-4-7':    'oklch(0.72 0.16 25)',   // coral
  'opus-4-6':    'oklch(0.78 0.14 55)',   // amber
  'opus-4-5':    'oklch(0.85 0.14 90)',   // gold
  'sonnet-5':    'oklch(0.75 0.15 275)',  // indigo
  'sonnet-4-6':  'oklch(0.78 0.14 245)',  // blue
  'sonnet-4-5':  'oklch(0.72 0.16 305)',  // violet
  'haiku-4-5':   'oklch(0.78 0.14 175)',  // teal — matches --accent
  '<synthetic>': 'oklch(0.65 0.02 260)',  // neutral
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

// Per-character advance of the dashboard's monospace face, measured once per
// font size and cached.
//
// It must be measured with getComputedTextLength() on a real SVG text node
// under a .dashboard ancestor, because app.css applies
//   .dashboard svg text { font-family: var(--mono); letter-spacing: 0.04em }
// and letter-spacing is invisible to canvas measureText. At 11px that is
// 0.44px/char — canvas reports 6.6px/char while the browser actually renders
// 7.04px/char. The old hard-coded 6.6 constant was exactly that canvas
// figure, and the missing 6.7% came out of the left margin: HBar's longest
// model label sat 3.6px from the panel edge.
function monoAdvancePx(fontSize) {
  monoAdvancePx._c = monoAdvancePx._c || {};
  if (monoAdvancePx._c[fontSize]) return monoAdvancePx._c[fontSize];
  const NS = 'http://www.w3.org/2000/svg';
  const host = document.createElement('div');
  host.className = 'dashboard';
  host.style.cssText =
    'position:absolute;left:-9999px;top:0;height:0;overflow:hidden;visibility:hidden';
  const svg = document.createElementNS(NS, 'svg');
  const text = document.createElementNS(NS, 'text');
  text.setAttribute('font-size', String(fontSize));
  text.setAttribute('font-family', 'monospace');
  const SAMPLE = '0123456789';
  text.textContent = SAMPLE;
  svg.appendChild(text);
  host.appendChild(svg);
  document.body.appendChild(host);
  // visibility:hidden still lays out, so this is a real measurement.
  const adv = text.getComputedTextLength() / SAMPLE.length;
  document.body.removeChild(host);
  // The face is monospace, so width === length * advance exactly; verified
  // against 8-, 9- and 10-character labels, all 7.041px/char at 11px.
  return (monoAdvancePx._c[fontSize] = adv > 0 ? adv : fontSize * 0.64);
}

function fmtDate(ts, opts = {}) {
  const d = new Date(ts);
  const M = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  if (opts.month) return M[d.getUTCMonth()] + ' ' + d.getUTCFullYear();
  if (opts.day) return M[d.getUTCMonth()] + ' ' + d.getUTCDate();
  if (opts.full) return `${M[d.getUTCMonth()]} ${String(d.getUTCDate()).padStart(2,'0')} ${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}`;
  return d.toISOString();
}

// Adaptive UTC x-axis ticks: months when the span is wide, day starts
// for medium spans, hours for narrow ones. Month-only ticks (the old
// behavior in every panel) left the x-axis completely unlabeled on the
// 24h / 7d / 30d range presets. Returns [{ts, label}].
function timeTicksUTC(start, end) {
  const HOUR = 3600_000, DAY = 24 * HOUR;
  const span = Math.max(1, end - start);
  const ticks = [];
  if (span >= 80 * DAY) {
    // Month starts, thinned to ≤ ~12 labels.
    const all = [];
    const d = new Date(start);
    let m = d.getUTCMonth(), y = d.getUTCFullYear();
    for (let it = 0; it < 60; it++) {
      const t = Date.UTC(y, m, 1);
      if (t > end) break;
      if (t > start) all.push(t);
      m++; if (m > 11) { m = 0; y++; }
    }
    const step = Math.max(1, Math.ceil(all.length / 12));
    for (let i = 0; i < all.length; i += step) {
      ticks.push({ ts: all[i], label: fmtDate(all[i], { month: true }) });
    }
  } else if (span >= 3 * DAY) {
    // Day starts.
    const stepDays = span > 50 * DAY ? 7 : span > 25 * DAY ? 4 : span > 12 * DAY ? 2 : 1;
    const d0 = new Date(start);
    let t = Date.UTC(d0.getUTCFullYear(), d0.getUTCMonth(), d0.getUTCDate());
    while (t <= end) {
      if (t > start) ticks.push({ ts: t, label: fmtDate(t, { day: true }) });
      t += stepDays * DAY;
    }
  } else {
    // Hour marks.
    const stepH = span > 36 * HOUR ? 6 : span > 18 * HOUR ? 3 : span > 8 * HOUR ? 2 : 1;
    let t = Math.ceil(start / (stepH * HOUR)) * (stepH * HOUR);
    while (t <= end) {
      const d = new Date(t);
      ticks.push({ ts: t, label: `${String(d.getUTCHours()).padStart(2, '0')}:00` });
      t += stepH * HOUR;
    }
  }
  return ticks;
}

// Human label for a bin width ("5m", "6h", "1d") — used by axis legends.
function binMsLabel(ms) {
  if (ms < HOUR_MS) return (ms / 60_000) + 'm';
  if (ms < 24 * HOUR_MS) return (ms / HOUR_MS) + 'h';
  return (ms / (24 * HOUR_MS)) + 'd';
}
const HOUR_MS = 3600_000;

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
    borderColor: tip.accent || undefined,
    pointerEvents: 'none',
    zIndex: 5,
    maxWidth: 280,
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
          <span className="chart-tooltip-key">{l[0]}</span>
          <span className="chart-tooltip-val" style={{ color: l[2] || undefined }}>{l[1]}</span>
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
  const [yLabelPx, setYLabelPx] = React.useState(0);

  // Same reasoning as HBar: measure the rendered labels rather than predict
  // them from a font metric. Runs before paint, so the corrected padding is
  // never a visible reflow.
  React.useLayoutEffect(() => {
    if (!ref.current) return;
    let m = 0;
    ref.current.querySelectorAll('text[data-yl-label]').forEach(t => {
      const len = t.getComputedTextLength ? t.getComputedTextLength() : 0;
      if (len > m) m = len;
    });
    if (m > 0 && Math.abs(m - yLabelPx) > 0.5) setYLabelPx(m);
  });

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
  // padR is the right gutter: it holds the cumulative-axis labels (start-
  // anchored at padR - 6 from the edge) and the rotated "cumulative" title
  // whose box sits at w-21..w-9. At the old 50 the labels had 23px before
  // the title and "100M" is exactly 23.0px wide, so wider values collided.
  const padR = 70, padT = 28, padB = 28;
  // The left gutter holds the rotated "per <bin>" caption (box 8..20 once
  // anchored at x=17) and then the y labels, end-anchored at padL - 6. So
  // the widest label needs padL - 6 - width >= 26 to keep a 6px gap from the
  // caption. At a fixed 50 that held for "10M" but not for "$500.00", which
  // renders 40.3px wide and ended up 3.7px from the panel edge — measured,
  // not estimated, for the same reason as HBar below.
  const padL = Math.min(
    Math.max(60, w * 0.25),
    Math.max(50, Math.ceil(yLabelPx) + 32)
  );
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

  const ticks = timeTicksUTC(range.start, range.end);

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
    // The last bin starts at (or past) the plot's right edge whenever the
    // range isn't an exact multiple of binMs, so its bar body sits in the
    // right padding. Without widening the band by one bar, that bar has a
    // 0px-wide hover zone and simply cannot be inspected.
    if (mx < padL || mx > w - padR + barW || my < padT || my > padT + plotH) {
      setTip(null);
      return;
    }
    // Snap to the nearest bar centre instead of flooring the time fraction.
    // Flooring hands the final (usually partial) bin a sliver of a zone
    // while every other bin gets a full pitch.
    const pitch = plotW * binMs / Math.max(1, range.end - range.start);
    let idx = Math.round((mx - padL - barW / 2) / pitch);
    if (idx < 0) idx = 0;
    if (idx >= bins.length) idx = bins.length - 1;
    const b = bins[idx];
    const cum = cumPts[idx + 1];  // +1 to skip the leading (range.start, 0) anchor
    setTip({
      x: mx, y: my, idx,
      title: `${fmtDate(b.start, {full:true})} – ${fmtDate(b.end, {full:true})} UTC`,
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
      <svg data-panel={title} width={w} height={h} style={{ display: 'block' }}>
        {yTicksL.map((v, idx) => (
          <line key={'g'+idx} x1={padL} x2={w - padR}
            y1={yBar(v)} y2={yBar(v)}
            stroke={TH.grid} strokeOpacity="0.3" strokeWidth="1" />
        ))}
        {bins.map((b, idx) => {
          const x = xScale(b.start);
          const y = yBar(b.sum);
          const isHover = tip && tip.idx === idx;
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
          <text data-yl-label="" key={'yl'+idx} x={padL - 6} y={yBar(v) + 4}
            fontSize="9" fill={TH.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt(v, isCurrency)}
          </text>
        ))}
        {/* Start-anchored so labels grow rightward, AWAY from the plot: the
            final bar is drawn past the plot edge into the padding, so a
            right-anchored label grows left straight through it. The gutter
            (padR wide) has to fit the widest label plus the rotated
            "cumulative" title at x 849; padR is sized for that. */}
        {yTicksR.map((v, idx) => (
          <text key={'yr'+idx} x={w - padR + 6} y={yCum(v) + 4}
            fontSize="9" fill={TH.textDim} textAnchor="start" fontFamily="monospace">
            {humanFmt(v, isCurrency)}
          </text>
        ))}
        {ticks.map((t, idx) => (
          <text key={'x'+idx} x={xScale(t.ts)} y={h - padB + 14}
            fontSize="9" fill={TH.textDim} textAnchor="middle" fontFamily="monospace">
            {t.label}
          </text>
        ))}

        <text x={w/2} y={18} fontSize="13" fontWeight="bold" fill={TH.text}
          textAnchor="middle" fontFamily="monospace">{title}</text>

        {/* x=17 not 12: at 12 the rotated caption's box started 3px from the
            panel edge. 17 puts it at 8px, matching the margin the y labels
            get on the other side of it. */}
        <text x={17} y={padT + plotH/2} fontSize="9" fill={TH.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 17 ${padT + plotH/2})`}>per {binMsLabel(binMs)}</text>
        <text x={w - 12} y={padT + plotH/2} fontSize="9" fill={TH.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 ${w - 12} ${padT + plotH/2})`}>cumulative</text>

        {(() => {
          const totalStr = `Total: ${humanFmt(total, isCurrency)}`;
          const boxW = Math.ceil(totalStr.length * monoAdvancePx(11)) + 16;
          const boxX = w - padR - boxW - 6;
          return (
            <g>
              <rect x={boxX} y={padT + 2} width={boxW} height={20} rx={4}
                fill={TH.bgAxes} stroke={color} strokeOpacity="0.8" />
              <text x={boxX + boxW / 2} y={padT + 16} fontSize="11" fontWeight="bold"
                fill={color} textAnchor="middle" fontFamily="monospace">
                {totalStr}
              </text>
            </g>
          );
        })()}
      </svg>
      <Tooltip tip={tip} />
    </div>
  );
}

// --- Horizontal bar chart ---
function HBar({ title, rows, totalForPct, fmt, fixedColors, embedded }) {
  const ref = React.useRef(null);
  const [w, setW] = React.useState(600);
  const [hover, setHover] = React.useState(null);
  const [mouse, setMouse] = React.useState({ x: 0, y: 0 });
  const [labelPx, setLabelPx] = React.useState(0);

  // Measure the labels as they actually render instead of predicting their
  // width from a font metric. A detached probe disagreed with the real thing
  // — it resolved the generic monospace face (6.489px/char) rather than the
  // 7.041px/char this dashboard's CSS applies via letter-spacing — and the
  // wrong figure then sat in a cache for the page's lifetime. This runs
  // before paint, so the corrected padding never shows up as a visible
  // reflow, and it self-corrects if the font or CSS ever changes.
  React.useLayoutEffect(() => {
    if (!ref.current) return;
    let m = 0;
    ref.current.querySelectorAll('text[data-hbar-label]').forEach(t => {
      const len = t.getComputedTextLength ? t.getComputedTextLength() : 0;
      if (len > m) m = len;
    });
    if (m > 0 && Math.abs(m - labelPx) > 0.5) setLabelPx(m);
  });

  React.useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(es => setW(es[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  // Rows render on a 36px pitch from padT — size the SVG to exactly
  // that (the old 40 + rows*44 left ~8px of dead space per row).
  const h = 32 + rows.length * 36 + 18;
  // Left pad fits the widest label. Labels are end-anchored at padL - 8, so
  // +20 buys that 8px plus a 12px margin to the panel edge. labelPx is the
  // measured width from the layout effect above; the font-metric estimate
  // only seeds the very first paint, before anything exists to measure.
  const estLabelPx = monoAdvancePx(11) *
    rows.reduce((m, r) => Math.max(m, (r.label || '').length), 0);
  const padL = Math.min(
    Math.max(60, w * 0.45),
    Math.ceil(Math.max(labelPx, estLabelPx)) + 20
  );
  const padR = 60, padT = 32;
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
      background: embedded ? 'transparent' : TH.bgAxes,
      border: embedded ? 'none' : `1px solid ${TH.border}`,
      borderRadius: 4, padding: 0, position: 'relative',
    }}
    onMouseMove={e => {
      const rect = ref.current.getBoundingClientRect();
      setMouse({ x: e.clientX - rect.left, y: e.clientY - rect.top });
    }}
    onMouseLeave={() => setHover(null)}>
      <svg data-panel={title} width={w} height={h} style={{ display: 'block' }}>
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
              <text data-hbar-label="" x={padL - 8} y={y + 18} fontSize="11" fill={TH.text}
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
function BurnRatePanel({ events, sessions, limitHits, range: propRange, windowBoundaries }) {
  const ref = React.useRef(null);
  const [size, setSize] = React.useState({ w: 1200, h: 360 });
  const [tip, setTip] = React.useState(null);
  const [legendAdv, setLegendAdv] = React.useState(0);
  const [yLabelPx, setYLabelPx] = React.useState(0);

  // Per-character advance of the legend's 10px monospace, plus the widest
  // rendered y label — both measured from nodes that actually painted. Same
  // reasoning as the axis gutters elsewhere: a predicted advance disagrees
  // with the real one because app.css adds letter-spacing that measurement
  // APIs outside the SVG don't see.
  React.useLayoutEffect(() => {
    if (!ref.current) return;
    const t = ref.current.querySelector('text[data-legend-item]');
    if (t && t.getComputedTextLength) {
      const n = (t.textContent || '').length;
      if (n) {
        const a = t.getComputedTextLength() / n;
        if (a > 0 && Math.abs(a - legendAdv) > 0.05) setLegendAdv(a);
      }
    }
    let m = 0;
    ref.current.querySelectorAll('text[data-yl-label]').forEach(e => {
      const len = e.getComputedTextLength ? e.getComputedTextLength() : 0;
      if (len > m) m = len;
    });
    if (m > 0 && Math.abs(m - yLabelPx) > 0.5) setYLabelPx(m);
  });

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

  // The prop range is derived from hourly bucket MIDPOINTS, so it
  // doesn't match what this chart plots (raw session start/end + limit
  // hits). Derive a data-envelope range here so sessions never escape
  // the plot box on the left and the chart never trails into empty
  // space on the right.
  const range = (() => {
    let lo = Infinity, hi = -Infinity;
    // Sessions (dots + EMA polylines) are plotted at midpoints, so the
    // range that makes them fill the plot is the min/max of midpoints.
    for (const s of sessions) {
      const mid = (s.start + s.end) / 2;
      if (mid < lo) lo = mid;
      if (mid > hi) hi = mid;
    }
    for (const lh of limitHits) { if (lh.ts < lo) lo = lh.ts; if (lh.ts > hi) hi = lh.ts; }
    if (lo === Infinity || lo === hi) return propRange;
    return { start: lo, end: hi };
  })();
  // Top is just title (no legend); bottom has x-tick labels + the legend.
  // padL sized from the measured y labels, not fixed at 60. Labels are
  // end-anchored at padL - 8 and the rotated "Tokens per hour…" caption
  // occupies roughly x 8..22, so the widest label needs padL - 8 - width to
  // stay clear of it; +40 keeps ~10px. At the old fixed 60 the current
  // widest tick ("100K", 25.6px) left only 4.4px — the same shape as the
  // 100M/cumulative collision on the right gutter, one character from
  // breaking.
  const padR = 30, padT = 30, padB = 56;
  const padL = Math.min(
    Math.max(60, w * 0.2),
    Math.max(60, Math.ceil(yLabelPx) + 40)
  );
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
      ctxEnd: s.ctxEnd != null ? s.ctxEnd : null,
      primary,
      sums,
      out_per_h:    sums.output / durH,
      input_per_h:  sums.input / durH,
      cc_per_h:     sums.cc / durH,
      cr_per_h:     sums.cr / durH,
      // Cost/h scaled by 100 so dots share the EMA's tokens-per-hour
      // log axis without needing a second scale: $1/h ≈ 100 tok/h ≈ same
      // visual band. Y-axis label calls out the dual meaning.
      cost_per_h_x100: (sums.cost / durH) * 100,
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

  // Axis range covers BOTH the EMA lines (tokens/h) AND the dot positions
  // (cost/h × 100), so pull both sets of values into the min/max.
  let allRates = [];
  for (const k of Object.keys(series)) allRates = allRates.concat(series[k].vals);
  for (const s of sessionData) allRates.push(s.cost_per_h_x100);
  allRates = allRates.filter(v => v > 0);
  const yMin = Math.max(1, Math.min(...allRates) * 0.3);
  const yMax = Math.max(...allRates) * 3;
  const logYMin = Math.log10(yMin), logYMax = Math.log10(yMax);
  const xScale = ts => padL + ((ts - range.start) / (range.end - range.start)) * plotW;
  const yScale = v => {
    // Clamp to [yMin, yMax] so out-of-range sessions sit on the plot
    // edge instead of leaking out the bottom into the legend strip
    // (`yMin * 0.1` floor caused the negative-fraction overshoot).
    const cv = Math.max(yMin, Math.min(yMax, v));
    return padT + plotH - ((Math.log10(cv) - logYMin) / (logYMax - logYMin)) * plotH;
  };

  const yTicks = [];
  for (let p = Math.ceil(logYMin); p <= Math.floor(logYMax); p++) yTicks.push(Math.pow(10, p));

  const xTicks = timeTicksUTC(range.start, range.end);

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
      const sx = xScale(s.mid), sy = yScale(s.cost_per_h_x100);
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
      const dotD = best ? Math.hypot(xScale(best.mid)-mx, yScale(best.cost_per_h_x100)-my) : 1e9;
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
      const ctxKnown = best.ctxEnd != null;
      const areaPts2 = ctxKnown
        ? Math.min(Math.max(best.ctxEnd / 4000, 25), 250)
        : 16;
      const dotR = Math.sqrt(areaPts2);
      const sizeNote = ctxKnown
        ? `${dotR.toFixed(1)}px (ctx ${humanFmt(best.ctxEnd)})`
        : `${dotR.toFixed(1)}px (ctx unknown)`;
      setTip({
        x: mx, y: my,
        title: 'Session ' + (best.idx + 1),
        accent: MODEL_COLORS[best.primary] || '#888',
        lines: [
          ['model',         best.primary],
          ['start',         fmtDate(best.start, {full:true})],
          ['duration',      best.durH < 1 ? (best.durH*60).toFixed(0)+'m' : best.durH.toFixed(1)+'h'],
          ['requests',      String(best.reqs)],
          ['ctx at end',    ctxKnown ? humanFmt(best.ctxEnd) : 'unknown'],
          ['out tok/hr',    humanFmt(best.out_per_h)],
          ['cache rd tok/hr', humanFmt(best.cr_per_h)],
          ['cost / hour',   '$' + (best.cost_per_h_x100 / 100).toFixed(2) + '/h'],
          ['est. cost',     '$' + best.sums.cost.toFixed(2)],
          ['dot radius',    sizeNote],
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
      <svg data-panel="Session Burn Rate" width={w} height={h} style={{ display: 'block' }}>
        <defs>
          <clipPath id="burn-plot-clip">
            <rect x={padL} y={padT} width={plotW} height={plotH} />
          </clipPath>
        </defs>
        <text x={w/2} y={20} fontSize="14" fontWeight="bold" fill={TH.text}
          textAnchor="middle" fontFamily="monospace">
          Session Burn Rate  |  {fmtDate(range.start, {day:true})} – {fmtDate(range.end, {day:true})}, {new Date(range.end).getUTCFullYear()} UTC  |  {sessions.length.toLocaleString()} sessions, {events.reduce((s,e)=>s+(e.requests==null?1:e.requests),0).toLocaleString()} requests
        </text>
        {yTicks.map((v, i) => (
          <text data-yl-label="" key={'yl'+i} x={padL - 8} y={yScale(v) + 4}
            fontSize="10" fill={TH.textDim} textAnchor="end" fontFamily="monospace">
            {humanFmt(v)}
          </text>
        ))}
        <g clipPath="url(#burn-plot-clip)">
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
        {sessionData.map((s, i) => {
          // Scale dot AREA by ctx-at-end-of-session.
          //   100k ctx → 25 area-pts²,  1M ctx → 250 area-pts²
          // When ctxEnd is null (analyst spec 2026-05-07: empty ctx_turns),
          // render a fixed small open circle instead of the old durH × 60
          // duration fallback — that fallback collapsed every kvalita
          // subagent-only / synthetic-trailing session to either max-r or
          // a meaningless duration-scaled size.
          const ctxKnown = s.ctxEnd != null;
          const areaPts2 = ctxKnown
            ? Math.min(Math.max(s.ctxEnd / 4000, 25), 250)
            : 16; // r ≈ 4 px sentinel for ctx-unknown
          const r = Math.sqrt(areaPts2);
          const isHover = tip && tip.title === 'Session ' + (s.idx + 1);
          return (
            <circle key={'sd'+i} cx={xScale(s.mid)} cy={yScale(s.cost_per_h_x100)}
              r={isHover ? r + 2 : r}
              fill={ctxKnown ? (MODEL_COLORS[s.primary] || '#888') : 'none'}
              fillOpacity={isHover ? 0.95 : 0.5}
              stroke={ctxKnown ? '#fff' : (MODEL_COLORS[s.primary] || '#888')}
              strokeOpacity={isHover ? 0.9 : (ctxKnown ? 0.3 : 0.85)}
              strokeWidth={isHover ? 1.5 : (ctxKnown ? 0.5 : 1.2)}
              strokeDasharray={ctxKnown ? undefined : '2,2'} />
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
        </g>
        {xTicks.map((t, i) => (
          <text key={'x'+i} x={xScale(t.ts)} y={h - padB + 14}
            fontSize="10" fill={TH.textDim} textAnchor="middle" fontFamily="monospace">
            {t.label}
          </text>
        ))}
        {/* x=18 not 14: rotated text's box extends about one ascent to the
            left of its baseline, so at 14 it began 3.5px from the edge. */}
        <text x={18} y={padT + plotH/2} fontSize="10" fill={TH.textDim}
          textAnchor="middle" fontFamily="monospace"
          transform={`rotate(-90 18 ${padT + plotH/2})`}>Tokens per hour (EMA) / 100 × Cost per hour</text>

        {/* Legend entries are laid out cumulatively from their own widths,
            not on a fixed pitch. At the old 130px pitch "Cache Create (EMA)"
            (18 chars = 115.2px, plus a 20px swatch and 6px gap = 141.2px)
            overran its slot and the NEXT entry's swatch was drawn 11.2px
            inside it. Advance is measured, seeded at 6.4 for first paint. */}
        {(() => {
          const items = Object.entries(series).map(([k, s]) => (
            { key: k, color: s.color, label: `${s.label} (EMA)` }));
          items.push({ key: '__ratelimit', color: '#ff3366', label: 'Rate limit hit' });
          const adv = legendAdv || 6.4;
          // ROW 16, not 13: a legend label's glyph box is ~13px tall, so a
          // 13px pitch made wrapped rows touch.
          const SWATCH = 20, GAP = 6, SPACING = 24, ROW = 16;
          // Wrap instead of running off the panel: at a 800px viewport the
          // five entries need ~680px against ~630px of usable width, and an
          // unwrapped row put text 23px outside the svg.
          const avail = Math.max(120, w - (padL + 20) - padR);
          let cx = 0, row = 0;
          const placed = items.map(it => {
            const wEntry = SWATCH + GAP + it.label.length * adv + SPACING;
            if (cx > 0 && cx + wEntry > avail) { row += 1; cx = 0; }
            const at = cx, r = row;
            cx += wEntry;
            return { ...it, at, row: r };
          });
          const nRows = row + 1;
          return (
            <g transform={`translate(${padL + 20}, ${h - 22 - (nRows - 1) * ROW})`}>
              {placed.map(it => (
                <g key={it.key} transform={`translate(${it.at}, ${it.row * ROW})`}>
                  <line x1={0} x2={SWATCH} y1={6} y2={6} stroke={it.color} strokeWidth="2" />
                  <text data-legend-item="" x={SWATCH + GAP} y={10} fontSize="10"
                    fill={TH.text} fontFamily="monospace">{it.label}</text>
                </g>
              ))}
            </g>
          );
        })()}
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
window.timeTicksUTC = timeTicksUTC;
