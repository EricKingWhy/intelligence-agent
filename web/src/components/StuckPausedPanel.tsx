/** stuck 暂停面板（`#968`）——与 `PausedPanel`（budget/deadline）分开的引导面板。
 *
 *  依据：`docs/PRD_AGENT_RUN_BUDGET_PAUSE_RESUME.md` §12 与
 *  `11_STREAMING_API_WEB_UI.md` §6.1——stuck 暂停的恢复前置是**相关 steer 或
 *  已检测到的环境 / policy 变更**，后端对 stuck 的 `budget_increase` 一律 409
 *  （`run_budget.py` 只接受 relevant_steer / environment_change / policy_change），
 *  所以本面板**不提供**「抬高绝对 ceiling → 同 run 恢复」控件，也不提供一键重试
 *  （MUST NOT，`11 §6.1`）。引导用户走两条正路：给新指示（steer，走 Composer 的
 *  Ctrl/Cmd+Enter，`ADR-0030 D10`），或做出环境 / 策略变更后恢复。
 *
 *  成熟产品同语义（OpenHands SDK Stuck Detector：检出 stuck 后「停止执行或采取
 *  纠正动作」，无通用自动重试；Claude Code Stop hook：继续必须附带理由）——
 *  「继续」必须有依据，不是无条件 retry。
 *
 *  纯展示组件：不 fetch、不改会话状态；面板不禁用 / 不遮挡 Composer（steer 输入
 *  通道保持可用）。样式复用 `pause-panel-*`（`styles/app.css`），零新增 token。 */

import { PauseCircle } from 'lucide-react';
import type { RunPausedInfo } from '../types';
import { CONTINUATION_SECTIONS, pauseReasonLabel } from '../lib/runBudget';

export interface StuckPausedPanelProps {
  paused: RunPausedInfo;
}

export function StuckPausedPanel({ paused }: StuckPausedPanelProps) {
  const continuation = paused.continuation;
  return (
    <div className="pause-panel" role="status" aria-live="polite">
      <div className="pause-panel-head">
        <PauseCircle size={14} aria-hidden="true" />
        <span>已因疑似卡住暂停——同一 run 的非终态收口，不是失败</span>
      </div>
      <div className="pause-panel-facts">
        原因 {paused.reason}（{pauseReasonLabel(paused)}）· 命中模式 {paused.trigger_dimension} ·
        预算版本 {paused.version} · 收口 {paused.closeout_source}
        {paused.trace_id ? ` · trace ${paused.trace_id}` : ''}
      </div>
      {continuation && (
        <ul className="pause-panel-continuation">
          {CONTINUATION_SECTIONS.map(({ key, label }) =>
            continuation[key].length > 0 ? (
              <li key={key}>
                {label}：{continuation[key].join('；')}
              </li>
            ) : null,
          )}
          <li>下一步：{continuation.next_safe_action}</li>
        </ul>
      )}
      {paused.resume_requirements.length > 0 && (
        <div className="pause-panel-facts">
          恢复前置条件：{paused.resume_requirements.join('；')}
        </div>
      )}
      <div className="pause-panel-facts">
        {
          '恢复方式：提供新指示（steer）——在下方输入框输入，按 Ctrl/Cmd+Enter 发送；或做出环境 / 策略变更后再恢复。恢复需要变更依据，本面板不提供一键恢复。'
        }
      </div>
    </div>
  );
}
