// Synthetic usage_events generator. Mirrors the shape of what the
// Postgres `usage_events` table would yield: one row per assistant
// API call. Designed to look like the reference graph: a slow ramp
// from Feb -> Apr, a steep climb in late Apr / early May, with two
// rate-limit hits near the right edge.

window.generateSyntheticData = function () {
  const MS = 1000, MIN = 60 * MS, HOUR = 60 * MIN, DAY = 24 * HOUR;
  const start = Date.UTC(2026, 1, 5, 12, 20); // Feb 5
  const end   = Date.UTC(2026, 4, 6, 3, 8);   // May 6
  const events = [];
  const limitHits = [];

  // Pricing per model (USD per token)
  const PRICING = {
    'opus-4-7':   { in: 5/1e6, out: 25/1e6, cc: 6.25/1e6, cr: 0.5/1e6 },
    'opus-4-6':   { in: 5/1e6, out: 25/1e6, cc: 6.25/1e6, cr: 0.5/1e6 },
    'opus-4-5':   { in: 5/1e6, out: 25/1e6, cc: 6.25/1e6, cr: 0.5/1e6 },
    'sonnet-4-6': { in: 3/1e6, out: 15/1e6, cc: 3.75/1e6, cr: 0.3/1e6 },
    'sonnet-4-5': { in: 3/1e6, out: 15/1e6, cc: 3.75/1e6, cr: 0.3/1e6 },
    'haiku-4-5':  { in: 1/1e6, out: 5/1e6,  cc: 1.25/1e6, cr: 0.1/1e6 },
    '<synthetic>':{ in: 0, out: 0, cc: 0, cr: 0 },
  };

  // Session-level intensity ramp. Volume grows roughly exponentially.
  function intensity(t) {
    const frac = (t - start) / (end - start); // 0..1
    // base ramp + late-stage hockey-stick
    return 0.05 + Math.pow(frac, 3.2) * 1.0 + (frac > 0.65 ? Math.pow((frac - 0.65)/0.35, 2.5) * 1.2 : 0);
  }

  // Walk through time generating sessions.
  let t = start;
  let sessionId = 0;
  // Mulberry32 RNG for reproducibility
  let seed = 0x5eed;
  const rng = () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let r = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r;
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };

  while (t < end) {
    const I = intensity(t);
    // Days where I < 0.15 might be skipped entirely (dead days early on)
    if (I < 0.12 && rng() < 0.6) {
      t += DAY * (0.5 + rng());
      continue;
    }
    // Pick session start with diurnal bias (more during 14-23 UTC)
    const dayOffset = Math.floor((t - start) / DAY);
    const sessStart = start + dayOffset * DAY + (10 + rng() * 14) * HOUR + rng() * HOUR;
    if (sessStart < t) { t += DAY; continue; }
    if (sessStart > end) break;
    sessionId++;

    // Session duration & intensity
    const durMin = (5 + rng() * 90) * (0.5 + I);
    const reqs = Math.max(3, Math.floor((10 + rng() * 80) * I));

    // Model choice: shifts over time. Late period is opus-heavy.
    const frac = (sessStart - start) / (end - start);
    let model;
    const r = rng();
    if (frac < 0.3) {
      model = r < 0.5 ? 'opus-4-6' : r < 0.7 ? 'sonnet-4-5' : 'haiku-4-5';
    } else if (frac < 0.7) {
      model = r < 0.65 ? 'opus-4-6' : r < 0.85 ? 'opus-4-7' : 'haiku-4-5';
    } else {
      model = r < 0.5 ? 'opus-4-7' : r < 0.85 ? 'opus-4-6' : r < 0.92 ? 'haiku-4-5' : '<synthetic>';
    }

    for (let i = 0; i < reqs; i++) {
      const ts = sessStart + (i / reqs) * durMin * MIN + (rng() - 0.5) * 30 * MS;
      if (ts > end) break;

      // Token shape — cache_read dominates by orders of magnitude
      const ramp = 0.3 + I * 1.5;
      const inputT  = Math.floor((20 + rng() * 80) * ramp);
      const outputT = Math.floor((300 + rng() * 1500) * ramp);
      const ccT     = Math.floor((1000 + rng() * 6000) * ramp);
      const crT     = Math.floor((20000 + rng() * 120000) * ramp * (1 + frac * 4));

      const p = PRICING[model] || PRICING['opus-4-6'];
      const cost = inputT * p.in + outputT * p.out + ccT * p.cc + crT * p.cr;

      events.push({
        ts,
        session_id: 'sess_' + sessionId,
        model,
        input_tokens: inputT,
        output_tokens: outputT,
        cache_create: ccT,
        cache_read: crT,
        ephemeral_5m: Math.floor(ccT * 0.7),
        ephemeral_1h: Math.floor(ccT * 0.3),
        cost_usd: cost,
        is_api_error: 0,
      });
    }

    // Advance time. Gap distribution: most short, some long (window boundaries).
    const gapR = rng();
    if (gapR < 0.05) t = sessStart + durMin * MIN + (5 + rng() * 6) * HOUR;
    else if (gapR < 0.3) t = sessStart + durMin * MIN + (1 + rng() * 3) * HOUR;
    else t = sessStart + durMin * MIN + (10 + rng() * 60) * MIN;
  }

  events.sort((a, b) => a.ts - b.ts);

  // Add 2 rate-limit hits near the right edge
  limitHits.push({ ts: end - 6 * DAY - 3 * HOUR, text: 'rate limit' });
  limitHits.push({ ts: end - 12 * HOUR, text: 'rate limit' });

  return { events, limitHits, range: { start, end } };
};
