import { useEffect, useRef, useState } from 'react';
import type { AgentEvent, ConversationState } from '../types';
import {
  getMemoryRecord,
  getSessionMemoryRecalls,
  type MemoryRecord,
  type SessionMemoryRecall,
} from '../lib/memoryV2Api';
import { describeMemoryError, listProjects } from '../lib/api';
import { formatMemoryTime } from '../lib/memory';

interface Props {
  conversation: ConversationState;
}

interface ActivityItems {
  updated: AgentEvent[];
  recalled: AgentEvent[];
}

/** Notices are derived from the append-only event stream; details are fetched only on disclosure. */
export function MemoryActivity({ conversation }: Props) {
  const processed = useRef(0);
  const [items, setItems] = useState<ActivityItems>({ updated: [], recalled: [] });

  useEffect(() => {
    const appended = conversation.events.slice(processed.current);
    processed.current = conversation.events.length;
    const updated = appended.filter((event) => event.type === 'memory/updated'
      && Number.isSafeInteger(event.data.count) && (event.data.count as number) > 0);
    const recalled = appended.filter((event) => event.type === 'memory/recalled');
    if (updated.length || recalled.length) {
      setItems((previous) => ({
        updated: [...previous.updated, ...updated],
        recalled: [...previous.recalled, ...recalled],
      }));
    }
    // `events` is intentionally mutated in place; eventsVersion is its append signal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.eventsVersion, conversation.session_id]);

  if (items.updated.length === 0 && items.recalled.length === 0) return null;
  return (
    <section className="memory-v2-activity" aria-label="记忆活动">
      {items.updated.map((event, index) => <MemoryUpdateNotice key={`updated-${event.seq ?? index}`} event={event} sessionId={conversation.session_id} />)}
      {items.recalled.map((event, index) => <RecallNotice key={`recalled-${event.seq ?? index}`} event={event} sessionId={conversation.session_id} />)}
    </section>
  );
}

function MemoryUpdateNotice({ event, sessionId }: { event: AgentEvent; sessionId: string }) {
  const [expanded, setExpanded] = useState(false);
  const [records, setRecords] = useState<MemoryRecord[] | null>(null);
  const [missingCount, setMissingCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const count = event.data.count as number;
  const ids = Array.isArray(event.data.memory_ids)
    ? [...new Set(event.data.memory_ids.filter((id): id is string => typeof id === 'string' && id.length > 0))]
    : [];

  const loadRecords = async () => {
    setLoading(true);
    setRecords(null);
    setError(null);
    if (ids.length === 0) {
      setRecords([]);
      setMissingCount(count);
      setLoading(false);
      return;
    }
    try {
      const results = await Promise.allSettled(ids.map((id) => getMemoryRecord(id)));
      const found = results.flatMap((result) => result.status === 'fulfilled' ? [result.value] : []);
      let failedIds = ids.filter((_, index) => results[index]?.status === 'rejected');
      if (failedIds.length > 0) {
        try {
          const project = (await listProjects()).find((candidate) => candidate.session_ids.includes(sessionId));
          if (project) {
            const projectResults = await Promise.allSettled(
              failedIds.map((id) => getMemoryRecord(id, project.id)),
            );
            found.push(...projectResults.flatMap((result) => result.status === 'fulfilled' ? [result.value] : []));
            failedIds = failedIds.filter((_, index) => projectResults[index]?.status === 'rejected');
          }
        } catch {
          // Keep the generic unavailable state when project authorization cannot be resolved.
        }
      }
      setRecords(found);
      setMissingCount(Math.max(count - found.length, failedIds.length));
      if (found.length === 0 && failedIds.length > 0) setError('无法读取当前可访问的记忆记录。');
    } finally {
      setLoading(false);
    }
  };

  const toggle = async () => {
    if (expanded) { setExpanded(false); return; }
    setExpanded(true);
    await loadRecords();
  };

  return (
    <article className="memory-v2-activity-card">
      <button className="memory-v2-activity-toggle" aria-expanded={expanded} onClick={() => void toggle()}>
        <span>已更新 {count} 条记忆</span><span>{expanded ? '收起' : '查看更新'}</span>
      </button>
      {expanded && <div className="memory-v2-activity-detail" aria-live="polite">
        {loading && <span role="status">正在读取已授权的记忆…</span>}
        {error && <span className="memory-v2-error" role="alert">{error}</span>}
        {records?.map((record) => <div className="memory-v2-activity-record" key={record.id}>
          <span>{record.status === 'deleted' ? '内容已删除' : record.content}</span>
          <small>{record.kind ?? '旧版'} · {record.version === null ? '旧版' : `v${record.version}`} · {formatMemoryTime(record.created_at)}</small>
        </div>)}
        {records?.length === 0 && !error && <span>这些记录当前不可见，或已被删除。</span>}
        {records && missingCount > 0 && <span className="memory-v2-hint">另有 {missingCount} 条记录当前不可见或已删除。</span>}
        {error && <button className="memory-v2-quiet" onClick={() => void loadRecords()}>重试</button>}
      </div>}
    </article>
  );
}

function RecallNotice({ event, sessionId }: { event: AgentEvent; sessionId: string }) {
  const [expanded, setExpanded] = useState(false);
  const [recalls, setRecalls] = useState<SessionMemoryRecall[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const loadRecall = async () => {
    setLoading(true);
    setRecalls(null);
    setError(null);
    try { setRecalls(await getSessionMemoryRecalls(sessionId)); }
    catch (cause) { setError(describeMemoryError(cause, '读取召回说明失败')); }
    finally { setLoading(false); }
  };

  const toggle = async () => {
    if (expanded) { setExpanded(false); return; }
    setExpanded(true);
    await loadRecall();
  };

  const runId = event.run_id ?? null;
  const explanations = recalls?.find((recall) => recall.seq === event.seq && recall.run_id === runId)?.memories ?? null;
  return (
    <article className="memory-v2-activity-card">
      <button className="memory-v2-activity-toggle" aria-expanded={expanded} onClick={() => void toggle()}>
        <span>记忆召回说明</span><span>{expanded ? '收起' : '查看原因'}</span>
      </button>
      {expanded && <div className="memory-v2-activity-detail" aria-live="polite">
        {loading && <span role="status">正在读取召回说明…</span>}
        {error && <span className="memory-v2-error" role="alert">{error}</span>}
        {recalls !== null && explanations === null && <span>此事件没有可用的召回说明。</span>}
        {explanations?.length === 0 && <span>此轮没有记录到记忆召回。</span>}
        {explanations?.map((item) => <div className="memory-v2-recall" key={`${item.memory_id}-${item.version}`}>
          <strong>{item.kind} · {item.scope} · v{item.version}</strong>
          <span>来源：{item.source_type}{item.source_session_id ? ` · 会话 ${item.source_session_id}` : ''}</span>
          {item.source_event_ids.length > 0 && <span>来源事件：{item.source_event_ids.join(', ')}</span>}
          <dl>{Object.entries(item.ranking).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
        </div>)}
        {error && <button className="memory-v2-quiet" onClick={() => void loadRecall()}>重试</button>}
      </div>}
    </article>
  );
}
