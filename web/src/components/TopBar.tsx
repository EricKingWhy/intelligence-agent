/** TopBar — App Bar of the single-frame shell (Phase 2, Brief §5).
 *
 * Left: product identity. Center: current session title + Run Pulse
 * (signature #1 — icon + color + text, never color-only). Right:
 * inspector collapse toggle + theme toggle.
 */

import { Activity, CircleDashed, Loader2, Moon, PanelRight, SquareCheckBig, SquareX, Sun } from 'lucide-react';
import { useEffect, useState } from 'react';
import { applyTheme, initTheme, type Theme } from '../lib/theme';
import { deriveRunPulse, type RunPulseState } from '../lib/runState';
import type { ConversationState } from '../types';

interface Props {
  conversation: ConversationState | null;
  streaming: boolean;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
}

const PULSE_CLASS: Record<RunPulseState, string> = {
  idle: 'pulse-idle',
  thinking: 'pulse-thinking',
  tool: 'pulse-tool',
  completed: 'pulse-completed',
  failed: 'pulse-failed',
};

/** Icon channel of signature #1 (icon + color + text, never color-only). */
const PULSE_ICON: Record<RunPulseState, typeof Activity> = {
  idle: CircleDashed,
  thinking: Loader2,
  tool: Loader2,
  completed: SquareCheckBig,
  failed: SquareX,
};

export function TopBar({ conversation, streaming, inspectorOpen, onToggleInspector }: Props) {
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

  const pulse = deriveRunPulse(conversation, streaming);
  const active = pulse.state === 'thinking' || pulse.state === 'tool';
  const PulseIcon = PULSE_ICON[pulse.state];

  return (
    <header className="appbar">
      <div className="appbar-left">
        <Activity size={16} className="appbar-logo" />
        <span className="appbar-title">Agent Harness Inspector</span>
      </div>

      <div className="appbar-center">
        {conversation && (
          <span className="appbar-session mono">
            {conversation.session_id.slice(0, 8)}
          </span>
        )}
        <span className={`run-pulse ${PULSE_CLASS[pulse.state]}`}>
          <PulseIcon size={12} aria-hidden="true" />
          {pulse.label}
          {active && elapsedSec > 0 && <span className="num"> · {elapsedSec}s</span>}
        </span>
      </div>

      <div className="appbar-right">
        <button
          className="icon-btn"
          onClick={onToggleInspector}
          aria-label={inspectorOpen ? '收起 Inspector' : '展开 Inspector'}
          aria-pressed={inspectorOpen}
          title={inspectorOpen ? '收起 Inspector' : '展开 Inspector'}
        >
          <PanelRight size={16} className={inspectorOpen ? 'appbar-toggle-active' : undefined} />
        </button>
        <button className="icon-btn" onClick={toggleTheme} aria-label="切换主题">
          {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
        </button>
      </div>
    </header>
  );
}
