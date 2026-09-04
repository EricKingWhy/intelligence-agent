/** ApprovalCard — inline warning-yellow glass card for human-in-the-loop approval.
 *
 * V1 seam: backend uses auto-approve, so this rarely shows in practice.
 * Layout reserved per Q14=A: tool name + args + risk + approve/deny buttons.
 * On action → POST /api/sessions/{id}/approve → card fades to terminal state.
 */

import { useState } from 'react';
import { ShieldAlert, Check, X } from 'lucide-react';
import type { ToolCall } from '../types';
import { postApproval } from '../lib/api';

interface Props {
  sessionId: string;
  tool: ToolCall;
}

export function ApprovalCard({ sessionId, tool }: Props) {
  const [decision, setDecision] = useState<'pending' | 'approved' | 'denied'>('pending');
  const [busy, setBusy] = useState(false);

  const decide = async (approved: boolean) => {
    setBusy(true);
    try {
      await postApproval(sessionId, approved);
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
        <code>{tool.name}</code>
        <pre className="approval-args">{JSON.stringify(tool.args, null, 2)}</pre>
      </div>
      {decision === 'pending' && (
        <div className="approval-actions">
          <button className="btn-primary approval-approve" disabled={busy} onClick={() => decide(true)}>
            <Check size={14} /> 批准
          </button>
          <button className="btn-ghost approval-deny" disabled={busy} onClick={() => decide(false)}>
            <X size={14} /> 拒绝
          </button>
        </div>
      )}
    </div>
  );
}
