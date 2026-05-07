// JSONL parser for Claude Code transcripts.
// Mirrors the shapes from parse_session.py: extracts structured events
// (user/assistant/tool_call/tool_result/thinking/agent_spawn) plus meta
// events (assistant_usage, system, queue-operation, attachment).

window.parseTranscript = function parseTranscript(text, opts) {
  const events = [];
  const meta = [];
  const lines = text.split('\n');
  const seenReq = new Map(); // requestId -> usage event (for streaming merge)
  const seenUuids = (opts && opts.seenUuids) || null; // optional cross-file dedup

  function mergeUsageMax(existing, incoming) {
    // Recursive max merge: numeric fields take max, nested dicts merge
    // key-by-key, non-numeric fields keep `existing` if present else
    // copy from `incoming`. Mirrors parse_session.py's _merge_usage_max.
    if (existing == null) return incoming;
    if (incoming == null) return existing;
    if (typeof existing === 'number' && typeof incoming === 'number') {
      return Math.max(existing, incoming);
    }
    if (typeof existing === 'object' && typeof incoming === 'object'
        && !Array.isArray(existing) && !Array.isArray(incoming)) {
      const out = { ...existing };
      for (const k of Object.keys(incoming)) {
        out[k] = (k in out) ? mergeUsageMax(out[k], incoming[k]) : incoming[k];
      }
      return out;
    }
    return existing; // type mismatch — keep existing
  }

  // Sniff a tool_result / user_message body for off-disk references:
  // - <task-notification>…<output-file>…</output-file>…</task-notification>
  //   pointing at a sibling agent-<id>.jsonl (subagent transcript)
  // - tool-results/<id>.<ext> paths (sidecar tool output files)
  // - bare agent-<id> references
  // Returns an array of { kind, ... } records or [] if none found.
  function detectRefs(text) {
    if (!text || typeof text !== 'string') return [];
    const refs = [];
    // Task notifications (subagent finished / event)
    const taskRe = /<task-notification>([\s\S]*?)<\/task-notification>/g;
    let m;
    while ((m = taskRe.exec(text)) !== null) {
      const body = m[1];
      const taskId   = (body.match(/<task-id>([^<]+)<\/task-id>/) || [])[1];
      const toolUse  = (body.match(/<tool-use-id>([^<]+)<\/tool-use-id>/) || [])[1];
      const outFile  = (body.match(/<output-file>([^<]+)<\/output-file>/) || [])[1];
      const event    = (body.match(/<event>([^<]+)<\/event>/) || [])[1];
      refs.push({
        kind: 'task_notification',
        task_id: taskId || '',
        tool_use_id: toolUse || '',
        output_file: outFile || '',
        event: event || '',
      });
    }
    // Sidecar tool-result files
    const fileRe = /(?:^|[^a-zA-Z0-9._-])(tool-results\/[A-Za-z0-9._-]+\.[a-zA-Z]+)/g;
    while ((m = fileRe.exec(text)) !== null) {
      refs.push({ kind: 'tool_result_file', path: m[1] });
    }
    // Bare agent IDs (only for explicit "agent-<hex>" with at least 12 hex chars)
    const agentRe = /\bagent-([a-f0-9]{12,})\b/g;
    const seenAgents = new Set();
    while ((m = agentRe.exec(text)) !== null) {
      if (seenAgents.has(m[1])) continue;
      seenAgents.add(m[1]);
      refs.push({ kind: 'agent_id', agent_id: m[1] });
    }
    return refs;
  }

  function pushUserContent(content, toolUseResult, lineNum, ts) {
    if (typeof content === 'string') {
      const refs = detectRefs(content);
      events.push({ line: lineNum, type: 'user_message', ts, detail: content, refs });
      return;
    }
    if (!Array.isArray(content)) return;
    for (const c of content) {
      if (c.type === 'tool_result') {
        let resultText = '';
        const raw = c.content;
        if (Array.isArray(raw)) {
          resultText = raw.map(x => (x && typeof x === 'object' ? (x.text || '') : String(x))).join('\n');
        } else {
          resultText = String(raw ?? '');
        }
        // Detect referenced sidecar files / subagent links inside the
        // result text (out-of-band attachments that live outside the JSONL).
        const refs = detectRefs(resultText);
        events.push({
          line: lineNum, type: 'tool_result', ts,
          tool_use_id: c.tool_use_id || '',
          is_error: !!c.is_error,
          detail: resultText,
          toolUseResult,
          refs,
        });
      } else if (c.type === 'text') {
        events.push({ line: lineNum, type: 'user_message', ts, detail: c.text });
      } else if (c.type === 'image') {
        events.push({ line: lineNum, type: 'user_message', ts, detail: '[image attachment]' });
      }
    }
  }

  function pushAssistantContent(content, lineNum, ts) {
    if (!Array.isArray(content)) return;
    for (const c of content) {
      if (c.type === 'text') {
        events.push({ line: lineNum, type: 'assistant_text', ts, detail: c.text });
      } else if (c.type === 'tool_use') {
        const ev = {
          line: lineNum,
          type: 'tool_call',
          ts,
          tool_name: c.name || '',
          tool_input: c.input || {},
          tool_use_id: c.id || '',
          detail: '',
        };
        if (c.name === 'Agent' || c.name === 'Task') {
          ev.type = 'agent_spawn';
          ev.agent_name = (c.input && c.input.name) || (c.input && c.input.subagent_type) || '?';
          ev.agent_model = (c.input && c.input.model) || '(default)';
          ev.agent_bg = !!(c.input && c.input.run_in_background);
          ev.agent_team = (c.input && c.input.team_name) || '(none)';
          ev.agent_prompt = (c.input && c.input.prompt) || '';
        }
        events.push(ev);
      } else if (c.type === 'thinking') {
        if (c.thinking) {
          events.push({ line: lineNum, type: 'thinking', ts, detail: c.thinking });
        }
      }
    }
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    let obj;
    try { obj = JSON.parse(line); } catch {
      events.push({ line: i + 1, type: 'parse_error', ts: '', detail: 'Invalid JSON' });
      continue;
    }

    // Cross-file dedup (directory / multi-load mode). Two files holding
    // the SAME API call — typically a session's main jsonl and one of its
    // agent-*.jsonl files — share an inner record `uuid`. Skip the second
    // occurrence so cost / turn / tool counts don't double-count.
    if (seenUuids) {
      const recUuid = obj.uuid;
      if (recUuid) {
        if (seenUuids.has(recUuid)) continue;
        seenUuids.add(recUuid);
      }
    }

    const ts = obj.timestamp || '';
    const msgType = obj.type || '';

    if (msgType === 'progress' || msgType === 'file-history-snapshot') continue;

    if (msgType === 'queue-operation') {
      meta.push({ line: i + 1, type: 'queue-operation', ts, operation: obj.operation || '', content: obj.content || '', raw: obj });
      continue;
    }
    if (msgType === 'system') {
      const content = obj.content || '';
      const subtype = obj.subtype || '';
      meta.push({ line: i + 1, type: 'system', ts, subtype, content, raw: obj });
      // Detect rate-limit hits from system content
      const lower = (content + ' ' + subtype).toLowerCase();
      if (lower.includes('rate limit') || lower.includes('rate_limit') || lower.includes('429')) {
        meta.push({ line: i + 1, type: 'rate_limit', ts, content, raw: obj });
      }
      continue;
    }
    if (msgType === 'attachment') {
      meta.push({ line: i + 1, type: 'attachment', ts, attachment_type: (obj.attachment && obj.attachment.type) || '', raw: obj });
      continue;
    }
    if (msgType === 'permission-mode') {
      meta.push({ line: i + 1, type: 'permission-mode', ts, permissionMode: obj.permissionMode || '', raw: obj });
      continue;
    }

    const m = obj.message || {};
    const role = m.role || '';
    const content = m.content || '';

    if (role === 'user') {
      pushUserContent(content, obj.toolUseResult, i + 1, ts);
    } else if (role === 'assistant') {
      const usage = m.usage;
      if (usage) {
        const reqId = obj.requestId || '';
        const ev = {
          line: i + 1, type: 'assistant_usage', ts,
          model: m.model || '(unknown)',
          requestId: reqId,
          uuid: obj.uuid || '',
          sessionId: obj.sessionId || '',
          usage: { ...usage },
        };
        if (reqId && seenReq.has(reqId)) {
          // Recursive max merge — handles streaming where output_tokens is
          // reported incrementally, plus nested cache_creation dict.
          const existing = seenReq.get(reqId);
          existing.usage = mergeUsageMax(existing.usage, usage);
        } else {
          if (reqId) seenReq.set(reqId, ev);
          meta.push(ev);
        }
      }
      pushAssistantContent(content, i + 1, ts);
    }
  }

  // Annotate parallel batches: consecutive tool calls < 2s apart
  const toolEvs = events.filter(e => e.type === 'tool_call' || e.type === 'agent_spawn');
  if (toolEvs.length) {
    const batches = [[toolEvs[0]]];
    for (let i = 1; i < toolEvs.length; i++) {
      const prev = batches[batches.length - 1].slice(-1)[0];
      const tp = Date.parse(prev.ts);
      const tc = Date.parse(toolEvs[i].ts);
      if (!isNaN(tp) && !isNaN(tc) && Math.abs(tc - tp) < 2000) {
        batches[batches.length - 1].push(toolEvs[i]);
      } else {
        batches.push([toolEvs[i]]);
      }
    }
    for (const b of batches) {
      b.forEach((e, idx) => { e.batch_size = b.length; e.batch_index = idx + 1; });
    }
  }

  // Link tool_call <-> tool_result
  const callMap = new Map();
  for (const e of events) {
    if ((e.type === 'tool_call' || e.type === 'agent_spawn') && e.tool_use_id) {
      callMap.set(e.tool_use_id, e);
    }
  }
  for (const e of events) {
    if (e.type === 'tool_result' && e.tool_use_id) {
      const call = callMap.get(e.tool_use_id);
      if (call) {
        e.paired_call = call;
        call.paired_result = e;
      }
    }
  }

  return { events, meta };
};

// Compute session-level stats
window.computeSessionStats = function (events, meta) {
  const stats = {
    firstTs: null, lastTs: null,
    userMsgs: 0, asstMsgs: 0, thinking: 0,
    toolCalls: 0, toolResults: 0, errorResults: 0,
    parallelBatches: 0, parallelCalls: 0,
    toolCounts: {},
    models: new Set(),
    fresh: 0, create: 0, read: 0, output: 0, eph5: 0, eph1h: 0,
    turns: 0,
    cost: 0,
  };

  for (const e of events) {
    const t = Date.parse(e.ts);
    if (!isNaN(t)) {
      if (stats.firstTs == null || t < stats.firstTs) stats.firstTs = t;
      if (stats.lastTs == null || t > stats.lastTs) stats.lastTs = t;
    }
    if (e.type === 'tool_call') {
      stats.toolCalls++;
      stats.toolCounts[e.tool_name] = (stats.toolCounts[e.tool_name] || 0) + 1;
    } else if (e.type === 'agent_spawn') {
      stats.toolCalls++;
      stats.toolCounts['Agent'] = (stats.toolCounts['Agent'] || 0) + 1;
    } else if (e.type === 'user_message') stats.userMsgs++;
    else if (e.type === 'assistant_text') stats.asstMsgs++;
    else if (e.type === 'tool_result') {
      stats.toolResults++;
      if (e.is_error) stats.errorResults++;
    } else if (e.type === 'thinking') stats.thinking++;
    if (e.batch_size > 1 && e.batch_index === 1) {
      stats.parallelBatches++;
      stats.parallelCalls += e.batch_size;
    }
  }

  // Mirrors backend/pricing.py — order matters (most-specific first
  // so 'claude-opus-4-7' doesn't misroute to 'claude-opus-4').
  const RATES = {
    'claude-opus-4-7':   { fresh: 5,    c5: 6.25,  c1h: 10,   read: 0.5,  out: 25 },
    'claude-opus-4-6':   { fresh: 5,    c5: 6.25,  c1h: 10,   read: 0.5,  out: 25 },
    'claude-opus-4-5':   { fresh: 5,    c5: 6.25,  c1h: 10,   read: 0.5,  out: 25 },
    'claude-opus-4-1':   { fresh: 15,   c5: 18.75, c1h: 30,   read: 1.5,  out: 75 },
    'claude-opus-4':     { fresh: 15,   c5: 18.75, c1h: 30,   read: 1.5,  out: 75 },
    'claude-sonnet-4-6': { fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
    'claude-sonnet-4-5': { fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
    'claude-sonnet-4':   { fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
    'claude-haiku-4-5':  { fresh: 1,    c5: 1.25,  c1h: 2,    read: 0.1,  out: 5 },
    'claude-3-7-sonnet-':{ fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
    'claude-3-5-sonnet-':{ fresh: 3,    c5: 3.75,  c1h: 6,    read: 0.3,  out: 15 },
    'claude-3-5-haiku-': { fresh: 0.8,  c5: 1.0,   c1h: 1.6,  read: 0.08, out: 4 },
    'claude-3-opus-':    { fresh: 15,   c5: 18.75, c1h: 30,   read: 1.5,  out: 75 },
    'claude-3-haiku-':   { fresh: 0.25, c5: 0.30,  c1h: 0.50, read: 0.03, out: 1.25 },
  };
  function rate(model) {
    for (const k of Object.keys(RATES)) if (model.includes(k)) return RATES[k];
    return RATES['claude-opus-4-7'];
  }

  for (const m of meta) {
    if (m.type !== 'assistant_usage') continue;
    stats.turns++;
    stats.models.add(m.model);
    const u = m.usage;
    const f = u.input_tokens || 0;
    const cc = u.cache_creation_input_tokens || 0;
    const cr = u.cache_read_input_tokens || 0;
    const o = u.output_tokens || 0;
    const eph5 = (u.cache_creation && u.cache_creation.ephemeral_5m_input_tokens) || 0;
    const eph1h = (u.cache_creation && u.cache_creation.ephemeral_1h_input_tokens) || 0;
    stats.fresh += f; stats.create += cc; stats.read += cr; stats.output += o;
    stats.eph5 += eph5; stats.eph1h += eph1h;

    const r = rate(m.model || '');
    const unsplit = Math.max(0, cc - eph5 - eph1h);
    stats.cost += (f * r.fresh + (eph5 + unsplit) * r.c5 + eph1h * r.c1h + cr * r.read + o * r.out) / 1_000_000;
  }

  stats.totalInput = stats.fresh + stats.create + stats.read;
  stats.hitRate = stats.totalInput ? (stats.read / stats.totalInput) * 100 : 0;
  return stats;
};
