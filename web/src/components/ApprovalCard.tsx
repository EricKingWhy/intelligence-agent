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
 * other errors → keep card pending, show error message, allow retry. */
import { useState } from 'react';
import { ShieldAlert, Check, X } from 'lucide-react';
import type { PendingApproval } from '../types';
import { postApproval, AlreadyResolvedError } from '../lib/api';

interface Props {
  sessionId: string;
  approval: PendingApproval;
}

export function ApprovalCard({ sessionId, approval }: Props) {
  const [decision, setDecision] = useState<'pending' | 'approved' | 'denied'>('pending');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <div className={`approval-card glass ${decision}`}>
      <div className="approval-header">
        <ShieldAlert size={16} className="approval-icon" />
        <span className="approval-title">
          {decision === 'pending' && '需要审批'}
          {decision === 'approved' && '已批准'}
          {decision === 'denied' && '已拒绝'}
        </span>
      </div>
      <div className="approval-tool">
        <code>{approval.tool_name}</code>
        <pre className="approval-args">
          {JSON.stringify(approval.arguments_preview ?? {}, null, 2)}
        </pre>
      </div>
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
            <Check size={14} /> 批准
          </button>
          <button
            className="btn-ghost approval-deny"
            disabled={busy}
            onClick={() => decide(false)}
          >
            <X size={14} /> 拒绝
          </button>
        </div>
      )}
    </div>
  );
}
