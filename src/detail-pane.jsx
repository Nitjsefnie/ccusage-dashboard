// Detail pane: rich rendering of selected event.

function EventDetail({ event, dense }) {
  if (!event) {
    return (
      <div style={{ padding: 24, color: 'var(--muted)', fontFamily: 'var(--sans)', fontSize: 13 }}>
        Select an event from the timeline to inspect it.
      </div>
    );
  }
  const meta = window.TYPE_META[event.type] || { label: event.type, color: 'var(--fg)' };
  const padY = dense ? 10 : 16;

  return (
    <div style={{ padding: `${padY}px 20px`, fontFamily: 'var(--sans)', color: 'var(--fg)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <span style={{
          fontFamily: 'var(--mono)', fontSize: 11, letterSpacing: 1,
          color: meta.color, padding: '2px 8px',
          border: `1px solid ${meta.color}40`, borderRadius: 4,
          background: `${meta.color}10`,
        }}>{meta.label}</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)' }}>
          L{event.line}  ·  {window.shortTime(event.ts)}
        </span>
        {event.batch_size > 1 && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--accent)' }}>
            ⫶ parallel {event.batch_index}/{event.batch_size}
          </span>
        )}
        {event.is_error && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'oklch(0.7 0.18 25)' }}>ERROR</span>
        )}
      </div>

      {event.type === 'user_message' && (
        <>
          <Plain text={event.detail} label="User" />
          <RefsBlock refs={event.refs} />
        </>
      )}
      {event.type === 'assistant_text' && <Plain text={event.detail} label="Assistant" />}
      {event.type === 'thinking' && <Thinking text={event.detail} />}
      {event.type === 'tool_call' && <ToolCallDetail event={event} />}
      {event.type === 'tool_result' && <ToolResultDetail event={event} />}
      {event.type === 'agent_spawn' && <AgentDetail event={event} />}
    </div>
  );
}

function Plain({ text, label }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>{label} message</div>
      <div style={{
        fontFamily: 'var(--sans)', fontSize: 14, lineHeight: 1.6,
        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        color: 'var(--fg)',
      }}>{text}</div>
    </div>
  );
}

function Thinking({ text }) {
  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 6, fontStyle: 'italic' }}>extended thinking</div>
      <div style={{
        fontFamily: 'var(--sans)', fontSize: 13, lineHeight: 1.65,
        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        color: 'oklch(0.78 0.04 290)',
        borderLeft: '2px solid oklch(0.55 0.06 290)',
        paddingLeft: 14,
        fontStyle: 'italic',
      }}>{text}</div>
    </div>
  );
}

function ToolCallDetail({ event }) {
  const name = event.tool_name;
  const input = event.tool_input || {};
  const result = event.paired_result;

  let body;
  // Bash + any wrapper tool that takes a `command` (Monitor, BashOutput, etc.)
  const isCommandLike = typeof input.command === 'string';
  if (name === 'Bash' || (isCommandLike && name !== 'Edit' && name !== 'Write' && name !== 'Read' && name !== 'Grep' && name !== 'TodoWrite')) {
    const timeoutLabel = input.timeout != null
      ? `${input.timeout}ms`
      : (input.timeout_ms != null ? `${input.timeout_ms}ms` : null);
    // Render any "extra" fields that aren't the well-known ones
    const known = new Set(['command', 'description', 'timeout', 'timeout_ms', 'run_in_background', 'persistent']);
    const extras = Object.keys(input).filter(k => !known.has(k));
    body = (
      <>
        {input.description && <window.KV k="description" v={input.description} />}
        <div style={{ marginTop: input.description ? 10 : 0, fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>command</div>
        <window.CodeBlock>{input.command || ''}</window.CodeBlock>
        {timeoutLabel && <div style={{ marginTop: 8 }}><window.KV k="timeout" v={timeoutLabel} /></div>}
        {input.run_in_background && <window.KV k="background" v="true" />}
        {input.persistent && <window.KV k="persistent" v="true" />}
        {extras.map(k => (
          <window.KV key={k} k={k} v={typeof input[k] === 'object' ? JSON.stringify(input[k]) : String(input[k])} />
        ))}
      </>
    );
  } else if (name === 'Edit') {
    body = (
      <>
        <window.KV k="file_path" v={input.file_path || '?'} />
        {input.replace_all && <window.KV k="replace_all" v="true" />}
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>diff</div>
        {window.renderEditDiff(input)}
      </>
    );
  } else if (name === 'Write') {
    body = (
      <>
        <window.KV k="file_path" v={input.file_path || '?'} />
        <window.KV k="size" v={`${(input.content || '').length} chars`} />
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>content</div>
        <window.CodeBlock max={4000}>{input.content || ''}</window.CodeBlock>
      </>
    );
  } else if (name === 'Read') {
    body = (
      <>
        <window.KV k="file_path" v={input.file_path || '?'} />
        {input.offset != null && <window.KV k="offset" v={String(input.offset)} />}
        {input.limit != null && <window.KV k="limit" v={String(input.limit)} />}
      </>
    );
  } else if (name === 'Grep') {
    body = (
      <>
        <window.KV k="pattern" v={input.pattern || ''} />
        {input.path && <window.KV k="path" v={input.path} />}
        {input.glob && <window.KV k="glob" v={input.glob} />}
        {input.output_mode && <window.KV k="output_mode" v={input.output_mode} />}
        {input['-i'] && <window.KV k="case insensitive" v="true" />}
      </>
    );
  } else if (name === 'TodoWrite') {
    body = (
      <div>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>todos</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {(input.todos || []).map((t, i) => {
            const c = t.status === 'completed' ? 'oklch(0.7 0.13 145)'
              : t.status === 'in_progress' ? 'oklch(0.78 0.13 60)'
              : 'oklch(0.55 0.02 90)';
            const g = t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '◐' : '○';
            return (
              <div key={i} style={{ display: 'flex', gap: 10, fontFamily: 'var(--mono)', fontSize: 12 }}>
                <span style={{ color: c }}>{g}</span>
                <span style={{ color: 'var(--fg)' }}>{t.content || t.activeForm || ''}</span>
              </div>
            );
          })}
        </div>
      </div>
    );
  } else {
    body = <window.CodeBlock max={4000}>{JSON.stringify(input, null, 2)}</window.CodeBlock>;
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', marginBottom: 14 }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 18, color: window.TOOL_COLORS[name] || 'var(--fg)' }}>{name}</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>{event.tool_use_id}</span>
      </div>
      {body}
      {result && (
        <div style={{ marginTop: 22, paddingTop: 16, borderTop: '1px dashed var(--border)' }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>
            ↩ result · L{result.line} · {window.shortTime(result.ts)}
            {result.is_error && <span style={{ color: 'oklch(0.7 0.18 25)', marginLeft: 10 }}>ERROR</span>}
            <span style={{ marginLeft: 10 }}>{(result.detail || '').length} chars</span>
          </div>
          <window.CodeBlock max={6000}>{result.detail || '(empty)'}</window.CodeBlock>
        </div>
      )}
    </div>
  );
}

function ToolResultDetail({ event }) {
  const call = event.paired_call;
  return (
    <div>
      {call && (
        <div style={{ marginBottom: 14, fontSize: 12, color: 'var(--muted)' }}>
          paired with <span style={{ fontFamily: 'var(--mono)', color: window.TOOL_COLORS[call.tool_name] || 'var(--fg)' }}>{call.tool_name}</span> at L{call.line}
        </div>
      )}
      <window.CodeBlock max={8000}>{event.detail || '(empty)'}</window.CodeBlock>
      <RefsBlock refs={event.refs} />
    </div>
  );
}

// External-resource references found inside an event body:
// - subagent task notifications (links to data/subagents/agent-<id>.jsonl)
// - tool-results sidecar files (data/tool-results/<id>.<ext>)
// - bare agent-<id> mentions
function RefsBlock({ refs }) {
  if (!refs || !refs.length) return null;
  // Group by kind
  const byKind = {};
  for (const r of refs) {
    if (!byKind[r.kind]) byKind[r.kind] = [];
    byKind[r.kind].push(r);
  }
  return (
    <div style={{
      marginTop: 18, padding: '12px 14px',
      background: 'oklch(0.22 0.02 270 / 0.4)',
      border: '1px solid var(--border)',
      borderRadius: 4,
    }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', fontFamily: 'var(--mono)',
        letterSpacing: 1, marginBottom: 8, textTransform: 'uppercase' }}>
        ↗ external references
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {(byKind.task_notification || []).map((r, i) => (
          <RefRow key={'tn'+i}
            kind="subagent task"
            color="oklch(0.78 0.16 290)"
            primary={r.event ? `event: ${r.event}` : 'task notification'}
            details={[
              ['task', r.task_id],
              ['tool_use', r.tool_use_id],
              r.output_file && ['output', r.output_file],
            ].filter(Boolean)}
          />
        ))}
        {(byKind.tool_result_file || []).map((r, i) => (
          <RefRow key={'tf'+i}
            kind="sidecar file"
            color="oklch(0.78 0.13 60)"
            primary={r.path}
            details={[]}
          />
        ))}
        {(byKind.agent_id || []).map((r, i) => (
          <RefRow key={'ag'+i}
            kind="agent"
            color="oklch(0.74 0.14 200)"
            primary={`agent-${r.agent_id}`}
            details={[]}
          />
        ))}
      </div>
    </div>
  );
}

function RefRow({ kind, color, primary, details }) {
  return (
    <div style={{ fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.55 }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
        <span style={{
          color, fontSize: 10, padding: '1px 7px', borderRadius: 3,
          border: `1px solid ${color}40`, background: `${color}10`,
          letterSpacing: 0.5,
        }}>{kind}</span>
        <span style={{ color: 'var(--fg)', wordBreak: 'break-all' }}>{primary}</span>
      </div>
      {details.length > 0 && (
        <div style={{ marginLeft: 8, marginTop: 4, color: 'var(--muted)', fontSize: 11 }}>
          {details.map(([k, v], i) => (
            <div key={i} style={{ display: 'flex', gap: 8 }}>
              <span style={{ color: 'var(--muted)', minWidth: 70 }}>{k}</span>
              <span style={{ color: 'var(--fg)', wordBreak: 'break-all' }}>{v}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AgentDetail({ event }) {
  return (
    <div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', marginBottom: 14 }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 18, color: window.TOOL_COLORS.Agent }}>Agent</span>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 13, color: 'var(--fg)' }}>{event.agent_name}</span>
      </div>
      <window.KV k="model" v={event.agent_model} />
      <window.KV k="background" v={String(event.agent_bg)} />
      <window.KV k="team" v={event.agent_team} />
      <div style={{ marginTop: 14, fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>prompt</div>
      <window.CodeBlock max={8000}>{event.agent_prompt || ''}</window.CodeBlock>
      {event.paired_result && (
        <div style={{ marginTop: 22, paddingTop: 16, borderTop: '1px dashed var(--border)' }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>
            ↩ result · L{event.paired_result.line}
          </div>
          <window.CodeBlock max={6000}>{event.paired_result.detail || ''}</window.CodeBlock>
        </div>
      )}
    </div>
  );
}

window.EventDetail = EventDetail;
