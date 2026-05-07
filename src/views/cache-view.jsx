// Cache view — literal replica of parse_session.py --cache output.
// Three sections:
//   1. PerModelTable: one row per model + a SESSION TOTAL row
//   2. TopTurnsTable × 3 (output, cache_create, cache_read)
//   3. CostBuckets: per-bucket cost with % + TOTAL + PER TURN
//
// Mounted by App.jsx as <window.CacheView project={...} range="30d" />.

window.CacheView = function CacheView({ project, range }) {
  const [data, setData] = React.useState(null);
  const [err, setErr] = React.useState(null);

  React.useEffect(() => {
    setData(null);
    setErr(null);
    const q = new URLSearchParams({ range });
    if (project) q.set('project', project);
    fetch(`/api/cache?${q}`, { credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then(setData)
      .catch(e => setErr(String(e)));
  }, [project, range]);

  if (err) return <div className="err">cache fetch failed: {err}</div>;
  if (!data) return <div className="loading">loading…</div>;

  return (
    <div className="cache-view">
      <h2>CACHE / TOKEN USAGE</h2>
      <PerModelTable rows={data.per_model} sessionTotal={data.session_total} />

      <h2>TOP 10 TURNS BY OUTPUT TOKENS</h2>
      <TopTurnsTable
        rows={data.top_output}
        cols={['ts', 'line', 'request_id', 'model', 'output',
               'c_read', 'c_create_1h', 'c_create_5m', 'fresh', 'cost']}
      />

      <h2>TOP 10 TURNS BY CACHE CREATE</h2>
      <TopTurnsTable
        rows={data.top_cache_create}
        cols={['ts', 'line', 'request_id', 'model', 'c_create',
               'c_create_1h', 'c_create_5m', 'c_read', 'output', 'fresh', 'cost']}
      />

      <h2>TOP 10 TURNS BY CACHE READ</h2>
      <TopTurnsTable
        rows={data.top_cache_read}
        cols={['ts', 'line', 'request_id', 'model', 'c_read',
               'c_create_1h', 'c_create_5m', 'output', 'fresh', 'cost']}
      />

      <h2>ESTIMATED API COST</h2>
      <CostBuckets
        buckets={data.session_total.cost_buckets}
        total={data.session_total.cost_total}
        turns={data.session_total.turns}
      />
    </div>
  );
};

function PerModelTable({ rows, sessionTotal }) {
  const fmt = window.humanFmt;
  const allRows = [
    ...rows,
    { ...sessionTotal, model: 'SESSION TOTAL' },
  ];
  return (
    <table className="cache-table">
      <thead>
        <tr>
          <th>model</th>
          <th>turns</th>
          <th>fresh</th>
          <th>cache_create</th>
          <th>cache_read</th>
          <th>output</th>
          <th>eph_5m</th>
          <th>eph_1h</th>
          <th>hit_rate</th>
          <th>cost</th>
        </tr>
      </thead>
      <tbody>
        {allRows.map((r, i) => (
          <tr key={r.model + i} className={r.model === 'SESSION TOTAL' ? 'total-row' : ''}>
            <td>{r.model}</td>
            <td>{r.turns.toLocaleString()}</td>
            <td>{fmt(r.fresh)}</td>
            <td>{fmt(r.cache_create)}</td>
            <td>{fmt(r.cache_read)}</td>
            <td>{fmt(r.output)}</td>
            <td>{fmt(r.eph5)}</td>
            <td>{fmt(r.eph1h)}</td>
            <td>{r.hit_rate_pct.toFixed(1)}%</td>
            <td>${r.cost_total.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TopTurnsTable({ rows, cols }) {
  return (
    <table className="cache-top-table">
      <thead>
        <tr>{cols.map(c => <th key={c}>{c}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            {cols.map(c => <td key={c}>{formatCell(r, c)}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatCell(r, c) {
  const v = r[c];
  if (v == null) return '';
  if (c === 'ts') return String(v).replace('T', ' ').slice(0, 19);
  if (c === 'cost') return '$' + Number(v).toFixed(3);
  if (c === 'request_id' || c === 'model' || c === 'file_key') return v;
  if (typeof v === 'number') return v.toLocaleString();
  return v;
}

function CostBuckets({ buckets, total, turns }) {
  const order = ['read', 'create_1h', 'create_5m', 'output', 'fresh'];
  const labels = {
    read: 'Cache read',
    create_1h: 'Cache create 1h',
    create_5m: 'Cache create 5m',
    output: 'Output',
    fresh: 'Fresh input',
  };
  return (
    <table className="cost-buckets">
      <thead>
        <tr><th>Category</th><th>Cost</th><th>%</th></tr>
      </thead>
      <tbody>
        {order.map(k => {
          const c = buckets[k] || 0;
          const pct = total > 0 ? (c / total * 100) : 0;
          return (
            <tr key={k}>
              <td>{labels[k]}</td>
              <td>${c.toFixed(2)}</td>
              <td>{pct.toFixed(1)}%</td>
            </tr>
          );
        })}
        <tr className="total-row">
          <td>TOTAL</td>
          <td>${total.toFixed(2)}</td>
          <td>100.0%</td>
        </tr>
        <tr>
          <td>PER TURN</td>
          <td>${(total / Math.max(turns, 1)).toFixed(4)}</td>
          <td>({turns.toLocaleString()} turns)</td>
        </tr>
      </tbody>
    </table>
  );
}
