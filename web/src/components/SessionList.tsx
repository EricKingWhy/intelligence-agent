/** SessionList — left rail listing historical sessions, newest first. */

import { Plus } from 'lucide-react';
import type { SessionSummary } from '../types';
import { formatRelativeTime } from '../lib/format';
import { useTickingNow } from '../hooks/useTickingNow';

interface Props {
  sessions: SessionSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}

export function SessionList({ sessions, selectedId, onSelect, onNew }: Props) {
  // 相对时间随时间流逝刷新（issue #40）：每分钟推进一次 now 触发重渲染。
  const now = useTickingNow();
  return (
    <aside className="session-list surface-panel">
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
        {sessions.map((s) => (
          <button
            key={s.session_id}
            className={`session-item ${selectedId === s.session_id ? 'selected' : ''}`}
            onClick={() => onSelect(s.session_id)}
            title={`${s.event_count} 事件`}
          >
            <span className="session-item-dot" />
            <div className="session-item-body">
              <div className="session-item-id">{s.session_id.slice(0, 12)}</div>
              <div className="session-item-meta">
                {s.event_count} 事件 · {formatRelativeTime(s.last_event_time, now)}
              </div>
            </div>
          </button>
        ))}
      </div>
    </aside>
  );
}
