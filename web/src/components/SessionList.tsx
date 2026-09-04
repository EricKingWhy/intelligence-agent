/** SessionList — Session Rail of the single-frame shell (Phase 2, Brief §10).
 *
 * Rows: pulse dot + short id + event count + relative time. Grouped by REAL
 * time semantics only — Running (the live stream's own session, known from
 * useSession state, not fabricated), Today / Yesterday / Older. No fake
 * organization (frozen decision E + Brief §10).
 */

import { Plus } from 'lucide-react';
import type { SessionSummary } from '../types';
import { formatRelativeTime } from '../lib/format';
import { useTickingNow } from '../hooks/useTickingNow';

interface Props {
  sessions: SessionSummary[];
  selectedId: string | null;
  /** The session currently receiving a live stream (Running group). */
  liveSessionId: string | null;
  /** 行标题缓存（Session Model E 轮）：键为 session_id，值为首条 user/message 投影。
   *  无缓存的会话回退到短 ID——不伪造标题。 */
  titlesById: Record<string, string>;
  onSelect: (id: string) => void;
  onNew: () => void;
}

type GroupName = 'Running' | 'Today' | 'Yesterday' | 'Older';
const GROUP_ORDER: GroupName[] = ['Running', 'Today', 'Yesterday', 'Older'];

export function SessionList({ sessions, selectedId, liveSessionId, titlesById, onSelect, onNew }: Props) {
  // 相对时间随时间流逝刷新（issue #40）：每分钟推进一次 now 触发重渲染。
  const now = useTickingNow();

  const groups = groupSessions(sessions, liveSessionId, now);

  return (
    <aside className="session-rail">
      <div className="session-list-header">
        <span className="panel-label">会话</span>
        <button className="icon-btn new-session-btn" onClick={onNew} aria-label="新建会话">
          <Plus size={14} />
        </button>
      </div>
      <div className="session-items">
        {sessions.length === 0 && (
          <div className="empty-hint">暂无会话，提交任务即可开始。</div>
        )}
        {groups.map(([name, items]) => (
          <div key={name} className="session-group">
            <div className="session-group-label">{name}</div>
            {items.map((s) => {
              const title = titlesById[s.session_id]?.slice(0, 48) || '';
              return (
                <button
                  key={s.session_id}
                  className={`session-item ${selectedId === s.session_id ? 'selected' : ''}`}
                  onClick={() => onSelect(s.session_id)}
                  title={`${s.session_id} · ${s.event_count} 事件`}
                >
                  <span
                    className={`session-item-dot ${name === 'Running' ? 'session-item-dot-live' : ''}`}
                    aria-hidden="true"
                  />
                  <div className="session-item-body">
                    {title && <div className="session-item-title">{title}</div>}
                    <div className="session-item-id mono">{s.session_id.slice(0, 12)}</div>
                    <div className="session-item-meta num">
                      {s.event_count} 事件 · {formatRelativeTime(s.last_event_time, now)}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </aside>
  );
}

/** Group sessions into real-time buckets; sessions outside any bucket's
 *  boundary (no timestamps) fall into Older. Order within a group follows the
 *  API's newest-first ordering — no re-sorting, no invented ordering. */
function groupSessions(
  sessions: SessionSummary[],
  liveSessionId: string | null,
  now: number,
): [GroupName, SessionSummary[]][] {
  const buckets: Record<GroupName, SessionSummary[]> = {
    Running: [],
    Today: [],
    Yesterday: [],
    Older: [],
  };
  const startOfDay = new Date(now);
  startOfDay.setHours(0, 0, 0, 0);
  const todayStart = startOfDay.getTime();
  const startOfYesterday = new Date(startOfDay);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  const yesterdayStart = startOfYesterday.getTime();

  for (const s of sessions) {
    if (liveSessionId && s.session_id === liveSessionId) {
      buckets.Running.push(s);
      continue;
    }
    const t = s.last_event_time ? new Date(s.last_event_time).getTime() : Number.NaN;
    if (Number.isNaN(t)) buckets.Older.push(s);
    else if (t >= todayStart) buckets.Today.push(s);
    else if (t >= yesterdayStart) buckets.Yesterday.push(s);
    else buckets.Older.push(s);
  }

  return GROUP_ORDER.filter((g) => buckets[g].length > 0).map((g) => [g, buckets[g]]);
}
