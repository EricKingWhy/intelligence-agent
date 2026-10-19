/** ContextWindowPanel — 「上下文窗口」挡位选择器（#1197 项 5B，效果图 01）。
 *
 *  外壳复用 `.ctx-usage-overlay` / `.ctx-usage-popover`（同一弹层惯例，先例
 *  `TaskReviewPanel` 的 `.task-review-panel`），只覆盖尺寸与内部排版。
 *
 *  三条口径（都是"界面是投影"的直接后果，不变量 #22）：
 *  - 挡位清单只来自后端 `tiers`（`CONTEXT_WINDOW_TIERS`），前端不硬编码副本——
 *    后端加一挡，这里自动多一个按钮。
 *  - 阈值是后端算好的**绝对 token 数**，原样显示；前端不拿 0.7/0.85 自己乘
 *    （那会让界面成为第二真相）。百分比只用于文案，由这两个数反推。
 *  - 写失败**不乐观改写**选中态：后端 422 是零写入，界面也必须停在原地
 *    （同 ContextUsagePanel 手动压缩的口径），失败文案直接用后端 detail。
 */

import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import {
  getContextSettings,
  setContextWindow,
  describeSessionError,
  type ContextSettings,
} from '../lib/api';

/** 挡位短标签：后端给的是 token 数，界面按 K 说（100_000 → 「100K」）。
 *  只做单位换算，不做取舍判断——显示哪个挡位由后端值决定。 */
function fmtTier(tokens: number): string {
  return `${Math.round(tokens / 1000)}K`;
}

export function ContextWindowPanel({
  sessionId,
  open,
  onClose,
}: {
  sessionId: string;
  open: boolean;
  onClose: () => void;
}) {
  const [settings, setSettings] = useState<ContextSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // 打开时拉一次（同 ContextUsagePanel：不做轮询）。setState 走 then 链，
  // 避免在打开的同一渲染里同步 setState 触发级联重渲染。
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    getContextSettings(sessionId).then(
      (s) => {
        if (cancelled) return;
        setLoadError(null);
        setSettings(s);
      },
      (e) => {
        if (!cancelled) setLoadError(describeSessionError(e, '加载失败'));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [open, sessionId]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  /** 改挡位：PATCH → 用回执（生效后的完整设置）刷新界面；失败只显示 detail。
   *  幂等：点当前挡位不会发请求（按钮已 aria-checked，重复写没意义）。 */
  const choose = async (tokens: number) => {
    if (saving || tokens === settings?.context_window) return;
    setSaving(true);
    setSaveError(null);
    try {
      setSettings(await setContextWindow(sessionId, tokens));
    } catch (e) {
      setSaveError(describeSessionError(e, '设置失败'));
    } finally {
      setSaving(false);
    }
  };

  const error = loadError ?? saveError;
  const window_ = settings?.context_window ?? 0;
  const pct = (v: number) => (window_ > 0 ? `${Math.round((v / window_) * 100)}%` : '—');

  return (
    <div className="ctx-usage-overlay" onClick={onClose}>
      <div
        className="ctx-usage-popover ctx-window-panel"
        role="dialog"
        aria-label="记忆容量"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="ctx-usage-head">
          <span className="ctx-usage-title">记忆容量</span>
          <button className="ctx-usage-close" onClick={onClose} aria-label="关闭">
            <X size={14} />
          </button>
        </div>

        {error && (
          <div className="ctx-window-error" role="alert">
            {error}
          </div>
        )}

        {!loadError && settings === null && <div className="ctx-usage-empty">加载中…</div>}

        {!loadError && settings && (
          <>
            {/* 文案以票面正文（方案 1）审批的提示语为准；效果图 01（mockup.html）
                写的是「窗口越大，压缩越晚；单次运行也会更慢、成本更高。」，两者同义
                不同文，这里按票面。 */}
            <p className="ctx-window-desc">记忆容量越大，AI 能记住的对话越多；耗时和成本取决于实际使用量。</p>
            <span className="ctx-window-badge">{fmtTier(settings.context_window)} 当前生效</span>

            <div className="ctx-window-choices" role="radiogroup" aria-label="记忆容量">
              {settings.tiers.map((t) => (
                <button
                  key={t}
                  type="button"
                  role="radio"
                  aria-checked={t === settings.context_window}
                  className={`ctx-window-choice ${t === settings.context_window ? 'sel' : ''}`}
                  disabled={saving}
                  onClick={() => void choose(t)}
                >
                  {fmtTier(t)}
                </button>
              ))}
            </div>

            {/* 阈值：后端算好的绝对值，原样显示（不变量 #22）。百分比只是给
                这两个数配一个"占窗口多少"的读法，由它们反推，不另立口径。 */}
            <div className="ctx-window-thresholds" aria-live="polite">
              <div className="ctx-window-threshold">
                <span>开始自动精简</span>
                <strong>{settings.thresholds.auto_compact.toLocaleString()}</strong>
                <span className="ctx-window-threshold-pct">
                  {pct(settings.thresholds.auto_compact)}
                </span>
              </div>
              <div className="ctx-window-threshold">
                <span>硬上限</span>
                <strong>{settings.thresholds.hard_guard.toLocaleString()}</strong>
                <span className="ctx-window-threshold-pct">
                  {pct(settings.thresholds.hard_guard)}
                </span>
              </div>
            </div>

            <p className="ctx-window-hint">
              到了自动精简线会先精简早期对话；到了硬上限就停止，不再继续增加内容。
            </p>

            {/* 「恢复默认」的落点是后端给的 configured_default（部署默认挡位），
                不是前端写死的 200K——部署改了默认值，这里跟着走。 */}
            <div className="ctx-window-foot">
              <button
                type="button"
                className="ctx-window-reset"
                disabled={saving}
                onClick={() => void choose(settings.configured_default)}
              >
                恢复 {fmtTier(settings.configured_default)}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
