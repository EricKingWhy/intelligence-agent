/** SessionList — left rail listing historical sessions, newest first. */

import { MessageSquare, Plus } from 'lucide-react';
import type { SessionSummary } from '../types';

interface Props {
  sessions: SessionSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}

function formatTime(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function SessionList({ sessions, selectedId, onSelect, onNew }: Props) {
  return (
    <aside className="session-list">
      <div className="session-list-header">
        <span className="panel-label">Sessions</span>
        <button className="icon-btn new-session-btn" onClick={onNew} aria-label="New session">
          <Plus size={14} />
        </button>
      </div>
      <div className="session-items">
        {sessions.length === 0 && (
          <div className="empty-hint">No sessions yet. Submit a task to begin.</div>
        )}
        {sessions.map((s) => (
          <button
            key={s.session_id}
            className={`session-item ${selectedId === s.session_id ? 'selected' : ''}`}
            onClick={() => onSelect(s.session_id)}
          >
            <MessageSquare size={14} className="session-item-icon" />
            <div className="session-item-body">
              <div className="session-item-id">{s.session_id.slice(0, 12)}</div>
              <div className="session-item-meta">
                {s.event_count} events · {formatTime(s.last_event_time)}
              </div>
            </div>
          </button>
        ))}
      </div>
    </aside>
  );
}
