/** TopBar — liquid glass chrome with app name, session meta, status. */

import { Activity, Moon, Sun } from 'lucide-react';
import { useEffect, useState } from 'react';
import { applyTheme, initTheme, type Theme } from '../lib/theme';

interface Props {
  sessionMeta?: { session_id: string; turn_count: number };
  streaming: boolean;
}

export function TopBar({ sessionMeta, streaming }: Props) {
  // 主题持久化：初始值由 lib/theme 在 paint 前解析（localStorage → 系统偏好）。
  const [theme, setTheme] = useState<Theme>(initTheme);

  // 流式计时（借鉴 ZCode 的"工作中 N 秒"）：进行中状态用真实秒数表达，而非空泛 spinner。
  const [elapsedSec, setElapsedSec] = useState(0);
  useEffect(() => {
    if (!streaming) {
      setElapsedSec(0);
      return;
    }
    const startedAt = Date.now();
    const timer = setInterval(
      () => setElapsedSec(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [streaming]);

  const toggleTheme = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    applyTheme(next);
  };

  return (
    <header className="topbar surface-panel">
      <div className="topbar-left">
        <Activity size={16} className="topbar-logo" />
        <span className="topbar-title">Agent Harness Inspector</span>
      </div>
      <div className="topbar-center">
        {streaming && (
          <span className="status-pill status-running">
            <span className="status-dot" /> 流式传输中
            {elapsedSec > 0 && <span className="num"> · {elapsedSec}s</span>}
          </span>
        )}
        {sessionMeta && !streaming && (
          <span className="status-pill">
            {sessionMeta.session_id.slice(0, 8)} · {sessionMeta.turn_count} 轮
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
