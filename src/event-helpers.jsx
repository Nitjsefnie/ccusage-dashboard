// Event row + detail rendering helpers for the Claude Code transcript visualizer.

const TOOL_COLORS = {
  Bash:    'oklch(0.78 0.13 60)',   // amber
  Read:    'oklch(0.72 0.10 230)',  // blue
  Edit:    'oklch(0.72 0.13 145)',  // green
  Write:   'oklch(0.72 0.14 145)',  // green
  Grep:    'oklch(0.72 0.10 280)',  // violet
  Glob:    'oklch(0.72 0.10 280)',
  Agent:   'oklch(0.78 0.15 25)',   // red-orange
  Task:    'oklch(0.78 0.15 25)',
  WebFetch:'oklch(0.72 0.10 200)',
  WebSearch:'oklch(0.72 0.10 200)',
  TodoWrite:'oklch(0.75 0.08 100)', // muted yellow
};

const TYPE_META = {
  user_message:   { label: 'USER',     color: 'oklch(0.85 0.12 75)',  glyph: '›' },
  assistant_text: { label: 'ASST',     color: 'oklch(0.92 0.02 80)',  glyph: '◆' },
  thinking:       { label: 'THINK',    color: 'oklch(0.55 0.04 290)', glyph: '~' },
  tool_call:      { label: 'TOOL',     color: 'oklch(0.78 0.13 60)',  glyph: '⏻' },
  tool_result:    { label: 'RESULT',   color: 'oklch(0.62 0.04 145)', glyph: '↩' },
  agent_spawn:    { label: 'AGENT',    color: 'oklch(0.78 0.15 25)',  glyph: '✦' },
  parse_error:    { label: 'PARSE',    color: 'oklch(0.65 0.18 25)',  glyph: '!' },
};

function shortTime(ts) {
  if (!ts) return '--:--:--';
  const d = new Date(ts);
  if (isNaN(d)) return '--:--:--';
  return d.toISOString().slice(11, 19);
}

function toolGlyph(name) {
  // Single character per tool for the timeline column
  const map = {
    Bash: '$', Read: '☰', Edit: '✎', Write: '✎',
    Grep: '⌕', Glob: '⌕', Agent: '✦', Task: '✦',
    WebFetch: '⌬', WebSearch: '⌬', TodoWrite: '☑',
    NotebookEdit: '✎', NotebookRead: '☰',
  };
  return map[name] || '·';
}

function inputPreview(name, input) {
  if (!input) return '';
  switch (name) {
    case 'Bash': {
      const cmd = (input.command || '').replace(/\s+/g, ' ');
      const desc = input.description ? ` — ${input.description}` : '';
      const bg = input.run_in_background ? ' [bg]' : '';
      return cmd + desc + bg;
    }
    case 'Read': {
      const off = input.offset ? ` :${input.offset}` : '';
      const lim = input.limit ? `+${input.limit}` : '';
      return (input.file_path || '?') + off + lim;
    }
    case 'Edit':
    case 'Write':
      return input.file_path || '?';
    case 'Grep': {
      const path = input.path ? ` in ${input.path}` : '';
      const glob = input.glob ? ` --glob ${input.glob}` : '';
      return `/${input.pattern || ''}/${path}${glob}`;
    }
    case 'Glob':
      return (input.pattern || '') + (input.path ? ` in ${input.path}` : '');
    case 'TodoWrite':
      return `${(input.todos || []).length} todos`;
    case 'WebFetch':
    case 'WebSearch':
      return input.url || input.query || '';
    case 'Agent':
    case 'Task':
      return `${input.subagent_type || input.name || '?'}: ${input.description || ''}`;
    default:
      try { return JSON.stringify(input).slice(0, 200); } catch { return ''; }
  }
}

function eventOneLine(e) {
  if (e.type === 'tool_call') {
    const tag = e.batch_size > 1 ? ` [${e.batch_index}/${e.batch_size}]` : '';
    return `${e.tool_name}${tag}: ${inputPreview(e.tool_name, e.tool_input)}`;
  }
  if (e.type === 'agent_spawn') {
    const tag = e.batch_size > 1 ? ` [${e.batch_index}/${e.batch_size}]` : '';
    return `Agent(${e.agent_name})${tag}: ${(e.agent_prompt || '').slice(0, 200)}`;
  }
  if (e.type === 'tool_result') {
    const call = e.paired_call || {};
    const pre = call.tool_name ? `← ${call.tool_name} ` : '';
    return pre + (e.detail || '').replace(/\s+/g, ' ');
  }
  return (e.detail || '').replace(/\s+/g, ' ');
}

// Compact diff renderer for Edit tool calls. Splits old/new on lines and
// emits a unified-diff-style block.
function renderEditDiff(input) {
  const oldLines = (input.old_string || '').split('\n');
  const newLines = (input.new_string || '').split('\n');
  return (
    <div style={{ fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.5 }}>
      {oldLines.map((l, i) => (
        <div key={'o' + i} style={{ background: 'oklch(0.32 0.06 25 / 0.35)', color: 'oklch(0.85 0.06 25)', padding: '0 8px', whiteSpace: 'pre-wrap' }}>
          <span style={{ opacity: 0.5, marginRight: 8 }}>−</span>{l}
        </div>
      ))}
      {newLines.map((l, i) => (
        <div key={'n' + i} style={{ background: 'oklch(0.30 0.05 145 / 0.35)', color: 'oklch(0.85 0.06 145)', padding: '0 8px', whiteSpace: 'pre-wrap' }}>
          <span style={{ opacity: 0.5, marginRight: 8 }}>+</span>{l}
        </div>
      ))}
    </div>
  );
}

function KV({ k, v }) {
  return (
    <div style={{ display: 'flex', gap: 12, fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.6 }}>
      <span style={{ color: 'var(--muted)', minWidth: 110 }}>{k}</span>
      <span style={{ color: 'var(--fg)', wordBreak: 'break-word' }}>{v}</span>
    </div>
  );
}

function CodeBlock({ children, max }) {
  const text = typeof children === 'string' ? children : '';
  const truncated = max && text.length > max;
  const display = truncated ? text.slice(0, max) + `\n\n… [${text.length - max} more chars]` : text;
  return (
    <pre style={{
      fontFamily: 'var(--mono)',
      fontSize: 12,
      lineHeight: 1.55,
      background: 'var(--code-bg)',
      color: 'var(--fg)',
      padding: '10px 12px',
      borderRadius: 6,
      border: '1px solid var(--border)',
      whiteSpace: 'pre-wrap',
      wordBreak: 'break-word',
      margin: 0,
      overflow: 'auto',
      maxHeight: max ? '60vh' : 'none',
    }}>{display}</pre>
  );
}

Object.assign(window, {
  TOOL_COLORS, TYPE_META, shortTime, toolGlyph, inputPreview, eventOneLine,
  renderEditDiff, KV, CodeBlock,
});
