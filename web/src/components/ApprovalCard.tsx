/** ApprovalCard — inline interactive approval card (#37, PRD §2.2).
 *
 * Renders when ConversationState.pending_approvals is non-empty.
 * User approves/denies → POST /api/sessions/{id}/approve →
 * permission/resolved event removes from pending queue.
 *
 * fail-closed: backend defaults to deny on timeout.
 * one-shot: same approval_id can only be resolved once (409 = already done).
 *
 * OBS-015 fix: the catch block used to flip the card to "approved/denied"
 * on ANY error — a dangerous false positive for security interactions.
 * Now: 409 (AlreadyResolvedError) → idempotent success, flip card;
 * other errors → keep card pending, show error message, allow retry.
 *
 * UI-01 重塑（PRD §UI-01，用户决策 D4：①②③⑤；倒计时 ④ 缓做）：
 *  - 结构化参数呈现（R7）：path 置顶可复制、command/content 原文块、
 *    old+new → DiffBlock；剩余键才 JSON 兜底——不再直出转义串。
 *  - description/reason 渲染（后端下发但此前未渲染）。
 *  - role="alertdialog" + 挂载焦点（多卡只有第一张聚焦，防焦点打架）。
 *  - 键盘路径：Ctrl/⌘+Enter 批准、Ctrl/⌘+Backspace 拒绝（按钮上以 kbd
 *    标注）。Esc 明确不占——它是全局中断运行，语义冲突。
 *  - 材质回归实心卡（R4 Glass Only Floating）：去 glass 类与第三级辉光。 */
import { useEffect, useRef, useState } from 'react';
import { ShieldAlert, Check, X } from 'lucide-react';
import type { PendingApproval } from '../types';
import { postApproval, AlreadyResolvedError } from '../lib/api';
import { classifyPreviewArgs } from '../lib/approvalPreview';
import { modKey } from '../lib/platform';
import { CopyButton } from './CopyButton';
import { DiffBlock } from './DiffBlock';

interface Props {
  sessionId: string;
  approval: PendingApproval;
  /** 多卡并存时只有第一张（Conversation 传 index===0）自动聚焦。 */
  autoFocus?: boolean;
}

export function ApprovalCard({ sessionId, approval, autoFocus = false }: Props) {
  const [decision, setDecision] = useState<'pending' | 'approved' | 'denied'>('pending');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  // 挂载聚焦一次即可：决策后不抢回焦点（用户可能已在别处操作）。
  useEffect(() => {
    if (autoFocus) cardRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const decide = async (approved: boolean) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await postApproval(sessionId, approval.approval_id, approved);
      setDecision(approved ? 'approved' : 'denied');
    } catch (e) {
      if (e instanceof AlreadyResolvedError) {
        // 409 = another tab or retry already resolved it; treat as our intent succeeding.
        setDecision(approved ? 'approved' : 'denied');
      } else {
        // Network failure / 5xx / etc.: decision did NOT reach backend.
        // Keep card pending so user can retry; show visible error.
        setError(e instanceof Error ? e.message : '审批请求失败');
      }
    } finally {
      setBusy(false);
    }
  };

  // 键盘路径（UI-01 ③）：仅 pending 且非 busy 时挂在 document 上；
  // decision/busy 变化即重挂/移除，决后快捷键失效。
  // 安全约束（U-1 review P1）：**只有 autoFocus 卡（第一张 pending 卡）挂全局
  // 监听**——否则 N 卡并存时一次 Ctrl+Enter 会向 N 个 approval_id 各发一 POST，
  // 等于一次按键批量批准多个危险操作。
  useEffect(() => {
    if (!autoFocus || decision !== 'pending' || busy) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat) return;
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        void decide(true);
      } else if ((e.ctrlKey || e.metaKey) && e.key === 'Backspace') {
        e.preventDefault();
        void decide(false);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
    // decide 闭包内的 busy 守卫由本 effect 的依赖（busy）保证新鲜。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoFocus, decision, busy]);

  const preview = classifyPreviewArgs(approval.arguments_preview);
  const desc = approval.description || approval.reason || '';
  const titleId = `approval-title-${approval.approval_id}`;
  const descId = desc ? `approval-desc-${approval.approval_id}` : undefined;
  const hasStructured =
    preview.command !== undefined ||
    preview.diff !== undefined ||
    preview.content !== undefined ||
    Object.keys(preview.rest).length > 0;
  const mod = modKey();

  return (
    <div
      ref={cardRef}
      className={`approval-card ${decision}`}
      role="alertdialog"
      aria-modal="false"
      aria-labelledby={titleId}
      aria-describedby={descId}
      tabIndex={-1}
    >
      <div className="approval-header">
        <ShieldAlert size={16} className="approval-icon" />
        <span className="approval-title" id={titleId}>
          {decision === 'pending' && '需要审批'}
          {decision === 'approved' && '已批准'}
          {decision === 'denied' && '已拒绝'}
        </span>
      </div>
      {desc && (
        <p className="approval-desc" id={descId}>
          {desc}
        </p>
      )}
      <div className="approval-tool">
        <code>{approval.tool_name}</code>
        {approval.permission && <span className="approval-chip">{approval.permission}</span>}
        {approval.policy && <span className="approval-chip">{approval.policy}</span>}
      </div>
      {preview.path && (
        <div className="approval-path">
          <code>{preview.path}</code>
          <CopyButton text={preview.path} label="复制路径" />
        </div>
      )}
      {hasStructured && (
        <div className="approval-preview">
          {preview.command && (
            <pre className="approval-cmd">
              <span className="approval-cmd-prompt">$ </span>
              {preview.command}
            </pre>
          )}
          {preview.diff && <DiffBlock diff={preview.diff} />}
          {preview.content && <pre className="approval-content">{preview.content}</pre>}
          {Object.keys(preview.rest).length > 0 && (
            <pre className="approval-args">{JSON.stringify(preview.rest, null, 2)}</pre>
          )}
        </div>
      )}
      {error && (
        <div className="approval-error" role="alert">
          {error}
        </div>
      )}
      {decision === 'pending' && (
        <div className="approval-actions">
          <button
            className="btn-primary approval-approve"
            disabled={busy}
            onClick={() => decide(true)}
          >
            <Check size={14} /> 批准 <kbd className="approval-kbd">{mod}+⏎</kbd>
          </button>
          <button
            className="btn-ghost approval-deny"
            disabled={busy}
            onClick={() => decide(false)}
          >
            <X size={14} /> 拒绝 <kbd className="approval-kbd">{mod}+⌫</kbd>
          </button>
        </div>
      )}
    </div>
  );
}
