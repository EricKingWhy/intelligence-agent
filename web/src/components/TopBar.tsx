/** TopBar — App Bar of the single-frame shell (Phase 2, Brief §5).
 *
 * Left: product identity. Center: current session title + Run Pulse
 * (signature #1 — icon + color + text, never color-only). Right:
 * trace density selector (four tiers, localStorage-persisted — frozen
 * decision), inspector collapse toggle + theme toggle.
 */

import { Activity, Moon, PanelRight, Sun } from 'lucide-react';
import { useEffect, useState } from 'react';
import { applyTheme, initTheme, type Theme } from '../lib/theme';
import { DENSITIES, type TraceDensity } from '../lib/density';
import { deriveRunPulse } from '../lib/runState';
import type { ConversationState } from '../types';

interface Props {
  conversation: ConversationState | null;
  streaming: boolean;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  /** Trace Density 四档（冻结决策）——状态归 App，这里只渲染切换控件。 */
  density: TraceDensity;
  onDensityChange: (d: TraceDensity) => void;
}

export function TopBar({ conversation, streaming, inspectorOpen, onToggleInspector, density, onDensityChange }: Props) {
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
  const PulseIcon = pulse.Icon;
  const active = pulse.state === 'thinking' || pulse.state === 'tool';

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
        <span className={`run-pulse ${pulse.className}`}>
          <PulseIcon size={12} aria-hidden="true" />
          {pulse.label}
          {active && elapsedSec > 0 && <span className="num"> · {elapsedSec}s</span>}
        </span>
      </div>

      <div className="appbar-right">
        <div className="density-picker" role="radiogroup" aria-label="Trace 密度">
          {DENSITIES.map((d) => (
            <button
              key={d}
              role="radio"
              aria-checked={density === d}
              className={`density-btn ${density === d ? 'sel' : ''}`}
              onClick={() => onDensityChange(d)}
              title={DENSITY_LABEL[d]}
            >
              {DENSITY_LABEL[d]}
            </button>
          ))}
        </div>
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

const DENSITY_LABEL: Record<TraceDensity, string> = {
  compact: '紧凑',
  balanced: '均衡',
  detailed: '详细',
  raw: 'Raw',
};
