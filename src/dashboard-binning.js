// Shared dashboard bin-width selection. The backend may aggregate more
// coarsely than the visible data span suggests, so frontend bins must never
// be finer than the server-provided bucket.

const DASHBOARD_NICE_BINS_MS = [
  60_000,
  5 * 60_000,
  15 * 60_000,
  30 * 60_000,
  60 * 60_000,
  6 * 60 * 60_000,
  12 * 60 * 60_000,
  24 * 60 * 60_000,
];

function pickAdaptiveBinMs(spanMs) {
  const span = Math.max(1, Number(spanMs) || 1);
  let chosen = DASHBOARD_NICE_BINS_MS[0];
  for (const width of DASHBOARD_NICE_BINS_MS) {
    if (span / width < 100) break;
    chosen = width;
  }
  return chosen;
}

function dashboardBinMs(range, bucketS) {
  const adaptive = pickAdaptiveBinMs(range.end - range.start);
  const serverMs = Number(bucketS) * 1000;
  return Number.isFinite(serverMs) && serverMs > 0
    ? Math.max(adaptive, serverMs)
    : adaptive;
}

function cacheTtlBinMs(range, dashboardMs) {
  const adaptive = pickAdaptiveBinMs(range.end - range.start);
  const floorMs = Number(dashboardMs);
  return Number.isFinite(floorMs) && floorMs > 0
    ? Math.max(adaptive, floorMs)
    : adaptive;
}

Object.assign(window, { pickAdaptiveBinMs, dashboardBinMs, cacheTtlBinMs });
