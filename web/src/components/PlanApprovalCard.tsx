/** #1356：计划审批——对话流**内嵌卡片**（对标 Claude Code 的 ExitPlanMode 审批面）。
 *
 *  与 `AskUserCard` 的分工：那个是运行中通用提问（`kind=ask_user`），这个是
 *  **计划提交**（`kind=plan_approval`）。两者共用同一条恢复通道
 *  （`resume_basis: 'user_input'`），但答案 wire 形态不同
 *  （`{questions:[…]}` vs `{decision, feedback?}`）——分流判据在 App
 *  （按 `USER_INPUT_REQUESTED.data.kind`）与投影（`pending_plan_approval`）。
 *
 *  为什么是**流内卡片**而不是模态：计划正文可能很长，用户需要一边往上翻对话
 *  （模型调研时说过什么）一边读计划；模态会把上下文挡在背后。同 `AskUserCard`
 *  的取舍。
 *
 *  纯展示 + 本地交互态：裁决经 `onSubmit` 交回 App 走 `resumePausedRun`，本组件
 *  不直接发请求、不持有会话真相。批准 = 用户显式动作，后端在恢复提交处把
 *  「切回 normal 档」与「续跑 run」当同一个原子动作（AC6），前端不需要也不该
 *  自己发切档请求。
 */
import { useEffect, useRef, useState } from 'react';
import { FileText } from 'lucide-react';
import type {
  PendingPlanApprovalRequest,
  PlanApprovalAnswer,
  PlanApprovalDecision,
} from '../types';
import { renderMarkdown } from '../lib/markdown';

export interface PlanApprovalCardProps {
  request: PendingPlanApprovalRequest;
  /** 恢复在途——禁用三个按钮（同 `AskUserCard` 的 `resuming`）。 */
  resuming?: boolean;
  /** 恢复失败原文（App 的会话错误）；null = 无错误。 */
  errorMessage?: string | null;
  onSubmit: (answer: PlanApprovalAnswer) => void;
}

export function PlanApprovalCard({
  request,
  resuming = false,
  errorMessage = null,
  onSubmit,
}: PlanApprovalCardProps) {
  const [feedback, setFeedback] = useState('');

  /* #1523 AC3：裁决提交后的焦点落点——同 `AskUserCard`。`resuming` 禁用三个按钮的
   * 瞬间浏览器把焦点丢给 body；成功则卡片卸载。提交即落 composer（App 的既有落点
   * 惯例 `#composer-input`）；失败（卡仍在、错误出现）拉回触发裁决的那个按钮。 */
  const submittedRef = useRef(false);
  const lastActionBtnRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!submittedRef.current || !errorMessage) return;
    lastActionBtnRef.current?.focus();
  }, [errorMessage]);

  /** 三个按钮**各点一次即提交**（AC9 的判据就是这一条）。反馈框常驻、可选：
   *  只在 revise / reject 时随裁决发出——后端对「approve + feedback」直接 422，
   *  所以批准时即使框里有字也不带（标签已写明适用范围，不是静默吞掉）。 */
  const submit = (decision: PlanApprovalDecision, btn: HTMLButtonElement | null) => {
    const trimmed = feedback.trim();
    onSubmit({
      request_id: request.request_id,
      decision,
      ...(decision !== 'approve' && trimmed ? { feedback: trimmed } : {}),
    });
    submittedRef.current = true;
    if (btn) lastActionBtnRef.current = btn;
    document.getElementById('composer-input')?.focus();
  };

  return (
    <div className="plan-approval-block">
      <div className="plan-approval-status" role="status" aria-live="polite">
        <span className="plan-approval-pulse" aria-hidden="true" />
        <FileText size={13} aria-hidden="true" />
        计划 · 等待批准
      </div>

      <section className="plan-approval-card" aria-label="计划审批">
        <div className="plan-approval-plan" data-testid="plan-approval-markdown">
          {renderMarkdown(request.plan_markdown)}
        </div>

        {request.touched_files.length > 0 && (
          <div className="plan-approval-files">
            <div className="plan-approval-files-title">将触碰 {request.touched_files.length} 个文件</div>
            <ul className="plan-approval-file-list">
              {request.touched_files.map((file) => (
                <li key={file} className="plan-approval-file">{file}</li>
              ))}
            </ul>
          </div>
        )}

        <textarea
          className="plan-approval-feedback"
          aria-label="计划反馈"
          value={feedback}
          onChange={(event) => setFeedback(event.target.value)}
          maxLength={100_000}
          placeholder="反馈（可选）：要求修改时说明要改哪里，拒绝时说明理由"
        />

        <div className="plan-approval-foot">
          <button
            type="button"
            className="plan-approval-btn primary plan-approval-approve"
            onClick={(e) => submit('approve', e.currentTarget)}
            disabled={resuming}
          >
            批准并开始执行
          </button>
          <button
            type="button"
            className="plan-approval-btn plan-approval-revise"
            onClick={(e) => submit('revise', e.currentTarget)}
            disabled={resuming}
          >
            要求修改
          </button>
          <button
            type="button"
            className="plan-approval-btn plan-approval-reject"
            onClick={(e) => submit('reject', e.currentTarget)}
            disabled={resuming}
          >
            拒绝
          </button>
        </div>

        {errorMessage && <div className="plan-approval-error" role="alert">{errorMessage}</div>}
      </section>
    </div>
  );
}
