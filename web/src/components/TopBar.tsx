/** TopBar — liquid glass chrome with app name, session meta, status. */

import { Activity, Moon, Sun } from 'lucide-react';
import { useState } from 'react';

interface Props {
  sessionMeta?: { session_id: string; event_count: number };
  streaming: boolean;
}

export function TopBar({ sessionMeta, streaming }: Props) {
  // Theme toggle is cosmetic — color-scheme is driven by prefers-color-scheme
  // via CSS. We toggle a data-theme attr on documentElement for future manual override.
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    document.documentElement.setAttribute('data-theme', next);
  };

  return (
    <header className="topbar glass">
      <div className="topbar-left">
        <Activity size={16} className="topbar-logo" />
        <span className="topbar-title">Agent Harness Inspector</span>
      </div>
      <div className="topbar-center">
        {streaming && (
          <span className="status-pill status-running">
            <span className="status-dot" /> 流式传输中
          </span>
        )}
        {sessionMeta && !streaming && (
          <span className="status-pill">
            {sessionMeta.session_id.slice(0, 8)} · {sessionMeta.event_count} 事件
          </span>
        )}
      </div>
      <div className="topbar-right">
        <button className="icon-btn" onClick={toggleTheme} aria-label="切换主题">
          {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
        </button>
      </div>
    </header>
  );
}
