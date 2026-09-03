/** SessionList — left rail listing historical sessions, newest first. */

import { Plus } from 'lucide-react';
import type { SessionSummary } from '../types';

interface Props {
  sessions: SessionSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}

function formatRelativeTime(iso: string | null): string {
  if (!iso) return '';
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function SessionList({ sessions, selectedId, onSelect, onNew }: Props) {
  return (
    <aside className="session-list surface-raised">
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
              <div className="session-item-meta num">
                {s.event_count} 事件 · {formatRelativeTime(s.last_event_time)}
              </div>
            </div>
          </button>
        ))}
      </div>
    </aside>
  );
}
