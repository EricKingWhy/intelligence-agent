/** TopBar — App Bar of the single-frame shell (Phase 2, Brief §5).
 *
 * Left: product identity. Center: current session title + Run Pulse
 * (signature #1 — icon + color + text, never color-only). Right:
 * trace density selector (four tiers, localStorage-persisted — frozen
 * decision), inspector collapse toggle + theme toggle.
 */

import { Activity, KeyRound, Moon, PanelRight, Sun } from 'lucide-react';
import { useEffect, useState } from 'react';
import { applyTheme, initTheme, type Theme } from '../lib/theme';
import { DENSITIES, type TraceDensity } from '../lib/density';
import { deriveRunPulse } from '../lib/runState';
import { getToken, setToken } from '../lib/auth';
import type { ConversationState } from '../types';

interface Props {
  conversation: ConversationState | null;
  streaming: boolean;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  /** Trace Density 四档（冻结决策）——状态归 App，这里只渲染切换控件。 */
  density: TraceDensity;
  onDensityChange: (d: TraceDensity) => void;
  /** 401 已发生（App 广播）——钥匙图标加提示点，引导配置 token。 */
  authRequired: boolean;
}

export function TopBar({ conversation, streaming, inspectorOpen, onToggleInspector, density, onDensityChange, authRequired }: Props) {
  // 主题持久化：初始值由 lib/theme 在 paint 前解析（localStorage → 系统偏好）。
  const [theme, setTheme] = useState<Theme>(initTheme);

  // Auth 设置面板：token 是开发者设置项（localStorage ahi.apiToken）。
  // 输入框只在面板打开时同步真值——打开 = 读取当前存储，避免陈旧副本。
  const [authPanelOpen, setAuthPanelOpen] = useState(false);
  const [tokenDraft, setTokenDraft] = useState('');

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

  const openAuthPanel = () => {
    setTokenDraft(getToken());
    setAuthPanelOpen((v) => !v);
  };

  const saveToken = () => {
    setToken(tokenDraft);
    setAuthPanelOpen(false);
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
          className={`icon-btn icon-btn-auth${authRequired ? ' attention' : ''}`}
          onClick={openAuthPanel}
          aria-label="API 身份令牌设置"
          aria-expanded={authPanelOpen}
          title="API 身份令牌（Bearer）——仅配置了 JWT_SECRET 的后端需要"
        >
          <KeyRound size={15} />
        </button>
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

      {authPanelOpen && (
        <div className="auth-panel" role="dialog" aria-label="API 身份令牌设置" onKeyDown={(e) => e.key === 'Escape' && setAuthPanelOpen(false)}>
          <div className="auth-panel-title">API 身份令牌（Bearer）</div>
          <input
            className="auth-panel-input"
            type="password"
            value={tokenDraft}
            placeholder="粘贴 HS256 token（eyJ…）"
            onChange={(e) => setTokenDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && saveToken()}
            autoFocus
          />
          <div className="auth-panel-hint">
            仅配置了 <code>JWT_SECRET</code> 的部署需要；本地开发留空即可。
            Token 保存在浏览器 localStorage（<code>ahi.apiToken</code>），claims 需含
            tenant_id / user_id / exp（未过期）。
          </div>
          <div className="auth-panel-actions">
            <button className="auth-panel-save" onClick={saveToken}>保存</button>
            <button
              className="auth-panel-clear"
              onClick={() => {
                setToken('');
                setAuthPanelOpen(false);
              }}
            >
              清除
            </button>
          </div>
        </div>
      )}
    </header>
  );
}

const DENSITY_LABEL: Record<TraceDensity, string> = {
  compact: '紧凑',
  balanced: '均衡',
  detailed: '详细',
  raw: 'Raw',
};
