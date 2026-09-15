/** ContextUsagePanel — 上下文容量看板（#200，设计稿 §5）。
 *
 * 形态（PORT DESIGN：deepseek-harness ContextMeter 占用条 + opencode
 * session-context-breakdown 分类桶模型，不复制代码文本）：
 * 上：分段条（消息/系统提示词/技能/其他 + 工具两组）+ 图例；
 * 中：阈值标记（70% auto compact / 85% hard guard——真实运行时行为）；
 * 下：缓存命中率一行（口径副标题「估算」）。
 *
 * 诚实约束（设计稿 §3）：空态/未采集**不显示 0%**；每个数字都是后端算出来的
 * （前端不自行推算，不变量 #22）；`estimated: true` ⇒ UI 标注「估算值」。
 * 溢出：小桶（<2%）按 opencode 的做法重标定显示宽度，但图例仍列出全部桶。
 */

import { memo, useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { getContextUsage, type ContextUsage } from '../lib/api';

/** 桶的展示元数据：key → 颜色 token + 中文名。顺序即图例顺序（与后端六桶一致）。 */
const BUCKETS: Array<{
  key: 'messages' | 'system_prompt' | 'skills' | 'other' | 'tools-system' | 'tools-mcp';
  label: string;
  colorVar: string;
  value: (u: ContextUsage) => number;
}> = [
  { key: 'messages', label: '消息', colorVar: 'var(--accent)', value: (u) => u.breakdown.messages },
  { key: 'system_prompt', label: '系统提示词', colorVar: 'var(--info)', value: (u) => u.breakdown.system_prompt },
  { key: 'skills', label: '技能', colorVar: 'var(--success)', value: (u) => u.breakdown.skills },
  { key: 'tools-system', label: '系统工具', colorVar: 'var(--warning)', value: (u) => u.breakdown.tools.system },
  { key: 'tools-mcp', label: 'MCP 工具', colorVar: 'var(--danger)', value: (u) => u.breakdown.tools.mcp },
  { key: 'other', label: '其他', colorVar: 'var(--text-tertiary)', value: (u) => u.breakdown.other },
];

/** 段宽度重标定（opencode 的做法）：小于 MIN_SEGMENT_PCT 的桶显示为最小宽度，
 *  图例仍列出全部桶（不隐藏数据）。 */
const MIN_SEGMENT_PCT = 1.5;

function pctOf(value: number, used: number): number {
  if (used <= 0) return 0;
  return (value / used) * 100;
}

export const ContextUsagePanel = memo(function ContextUsagePanel({
  sessionId,
  open,
  onClose,
}: {
  sessionId: string;
  open: boolean;
  onClose: () => void;
}) {
  const [usage, setUsage] = useState<ContextUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 刷新：打开时拉一次；会话有新 run 时不做实时轮询（设计稿 §5：避免无谓请求）。
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    // setError 在 then 链里（set-state-in-effect：同步 setState 会在浮层
    // 打开的同一渲染里触发级联重渲染，旧错误清理一并走这条链）。
    getContextUsage(sessionId)
      .then(
        (u) => {
          if (cancelled) return;
          setError(null);
          setUsage(u);
        },
        (e) => {
          if (!cancelled) setError((e as Error).message);
        },
      );
    return () => {
      cancelled = true;
    };
  }, [open, sessionId]);

  // Esc 关闭（与既有 picker 一致：弹层 Esc 可关）。
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

  return (
    <div className="ctx-usage-overlay" onClick={onClose}>
      <div
        className="ctx-usage-popover"
        role="dialog"
        aria-label="上下文容量"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="ctx-usage-head">
          <span className="ctx-usage-title">
            上下文容量
            <span className="ctx-usage-estimated">估算值</span>
          </span>
          <button className="ctx-usage-close" onClick={onClose} aria-label="关闭">
            <X size={14} />
          </button>
        </div>

        {error && (
          <div className="ctx-usage-error">加载失败：{error}</div>
        )}

        {!error && usage === null && (
          <div className="ctx-usage-empty">加载中…</div>
        )}

        {!error && usage && usage.state === 'no_data' && (
          <div className="ctx-usage-empty">暂无用量数据（会话还没有任何运行）</div>
        )}

        {!error && usage && usage.state === 'ok' && (
          <>
            <div className="ctx-usage-total">
              {usage.used_tokens.toLocaleString()} / {usage.window_tokens.toLocaleString()}
              {'（'}{pctOf(usage.used_tokens, usage.window_tokens).toFixed(1)}%{'）'}
            </div>

            {/* 分段条：role=img + aria-label 概述占比（无障碍，设计稿 §5）。
                小桶重标定显示宽度，图例仍列全部（不隐藏数据）。 */}
            <div
              className="ctx-usage-bar"
              role="img"
              aria-label={BUCKETS.map((b) => {
                const v = b.value(usage);
                return `${b.label} ${pctOf(v, usage.used_tokens).toFixed(1)}%`;
              }).join('、')}
            >
              {BUCKETS.map((b) => {
                const p = pctOf(b.value(usage), usage.used_tokens);
                if (p <= 0) return null;
                const width = Math.max(p, MIN_SEGMENT_PCT);
                return (
                  <div
                    key={b.key}
                    className="ctx-usage-seg"
                    style={{ width: `${width}%`, background: b.colorVar }}
                    title={`${b.label}：${b.value(usage).toLocaleString()} tok（${p.toFixed(1)}%）`}
                  />
                );
              })}
            </div>

            {/* 阈值标记：70% / 85% 在条上（真实运行时行为，config.py:69-70）。
                标记按「已用占窗口」的同一分母定位——用户应能看见自己在哪。 */}
            <div className="ctx-usage-thresholds">
              {([usage.thresholds.auto_compact, usage.thresholds.hard_guard] as const).map(
                (t, i) => (
                  <div
                    key={i}
                    className={`ctx-usage-mark ${i === 0 ? 'ctx-usage-mark-compact' : 'ctx-usage-mark-hard'}`}
                    style={{ left: `${t * 100}%` }}
                    title={
                      i === 0
                        ? `自动压缩阈值 ${Math.round(t * 100)}%`
                        : `硬保护阈值 ${Math.round(t * 100)}%`
                    }
                  />
                ),
              )}
            </div>

            <div className="ctx-usage-legend">
              {BUCKETS.map((b) => {
                const v = b.value(usage);
                const p = pctOf(v, usage.used_tokens);
                return (
                  <span key={b.key} className="ctx-usage-legend-item">
                    <span className="ctx-usage-dot" style={{ background: b.colorVar }} />
                    {b.label} {v.toLocaleString()}（{p.toFixed(1)}%）
                  </span>
                );
              })}
            </div>

            <div className="ctx-usage-cache">
              {usage.cache.state === 'not_collected' ? (
                /* 未采集：**不显示 0%**（诚实约束——设计稿 §3.1 无数据语义）。 */
                <span className="ctx-usage-cache-empty">
                  缓存命中率未采集（提供商未返回缓存明细）
                </span>
              ) : (
                <span className="ctx-usage-cache-value">
                  平均缓存命中率
                  {' '}
                  {usage.cache.avg_hit_rate !== null
                    ? `${(usage.cache.avg_hit_rate * 100).toFixed(1)}%`
                    : '—'}
                  <span className="ctx-usage-cache-note">
                    {'（'}{usage.cache.state === 'partial'
                      ? `${usage.cache.reported_calls}/${usage.cache.total_calls} 次调用带回明细`
                      : `${usage.cache.total_calls} 次调用全部带回明细`}{'}'} · 估算
                  </span>
                </span>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
});
