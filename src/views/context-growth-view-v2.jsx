// Context Growth view — replicates parse_session.py --context-growth.
// Two components:
//   window.ContextGrowthAgg          — distribution stats (per_turn + per_session_final)
//   window.ContextGrowthSessionDetail — per-turn array for one session

window.ContextGrowthAgg = function ContextGrowthAgg({ project, range }) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);

  React.useEffect(() => {
    setData(null);
    setErr(null);
    const q = new URLSearchParams({ range });
    if (project) q.set('project', project);
    fetch(`/api/context-growth/agg?${q}`, { credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, [project, range]);

  if (err) return <div className="err">context-growth/agg fetch failed: {err}</div>;
  if (!data) return <div className="loading">loading…</div>;

  return (
    <div className="ctx-agg">
      <h2>CONTEXT GROWTH — distribution</h2>
      <StatsRow label="per turn (input tokens)" stats={data.per_turn} />
      <StatsRow label="per session (final input tokens)" stats={data.per_session_final} />
    </div>
  );
};

function StatsRow({ label, stats }) {
  const fmt = window.humanFmt;
  return (
    <div className="ctx-stats-row">
      <h3>{label} <span className="muted">(n = {stats.n.toLocaleString()})</span></h3>
      <table className="ctx-stats-table">
        <thead>
          <tr>
            <th>mean</th><th>p50</th><th>p90</th><th>p99</th><th>max</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>{fmt(stats.mean)}</td>
            <td>{fmt(stats.p50)}</td>
            <td>{fmt(stats.p90)}</td>
            <td>{fmt(stats.p99)}</td>
            <td>{fmt(stats.max)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

window.ContextGrowthSessionDetail = function ContextGrowthSessionDetail({ sessionId }) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);

  React.useEffect(() => {
    setData(null);
    setErr(null);
    if (!sessionId) return;
    fetch(`/api/context-growth/session/${encodeURIComponent(sessionId)}`,
          { credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, [sessionId]);

  if (!sessionId) return null;
  if (err) return <div className="err">session fetch failed: {err}</div>;
  if (!data) return <div className="loading">loading session {sessionId}…</div>;

  const fmt = window.humanFmt;
  return (
    <div className="ctx-session-detail">
      <h2>CONTEXT GROWTH — {data.session_id}</h2>
      <table className="ctx-turns-table">
        <thead>
          <tr><th>#</th><th>time</th><th>L#</th><th>input</th><th>output</th><th>delta</th></tr>
        </thead>
        <tbody>
          {data.turns.map(t => (
            <tr key={t.idx}>
              <td>{t.idx}</td>
              <td>{String(t.ts).replace('T', ' ').slice(0, 19)}</td>
              <td>L{t.line}</td>
              <td>{t.input.toLocaleString()}</td>
              <td>{t.output.toLocaleString()}</td>
              <td>{(t.delta >= 0 ? '+' : '') + t.delta.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="ctx-summary">
        Total: {data.total_turns} turns, final context: {fmt(data.final_ctx)} input tokens
      </div>
    </div>
  );
};
