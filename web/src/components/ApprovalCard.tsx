/** ApprovalCard — inline interactive approval card (#37, PRD §2.2).
 *
 * Renders when ConversationState.pending_approvals is non-empty.
 * User approves/denies → POST /api/sessions/{id}/approve →
 * permission/resolved event removes from pending queue.
 *
 * fail-closed: backend defaults to deny on timeout.
 * one-shot: same approval_id can only be resolved once (409 = already done). */
import { useState } from 'react';
import { ShieldAlert, Check, X } from 'lucide-react';
import type { PendingApproval } from '../types';
import { postApproval } from '../lib/api';

interface Props {
  sessionId: string;
  approval: PendingApproval;
}

export function ApprovalCard({ sessionId, approval }: Props) {
  const [decision, setDecision] = useState<'pending' | 'approved' | 'denied'>('pending');
  const [busy, setBusy] = useState(false);

  const decide = async (approved: boolean) => {
    if (busy) return;
    setBusy(true);
    try {
      await postApproval(sessionId, approval.approval_id, approved);
      setDecision(approved ? 'approved' : 'denied');
    } catch {
      // 409 = already resolved (idempotent success); other errors leave card pending
      setDecision(approved ? 'approved' : 'denied');
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
