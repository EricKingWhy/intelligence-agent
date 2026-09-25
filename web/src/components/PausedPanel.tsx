/** 预算暂停面板（`#312` T4）——把一个**非终态**暂停的完整事实摊开，并给出唯一的
 *  恢复动作：抬高绝对 ceiling，然后以**同一 run_id** 续跑。
 *
 *  设计要点（每条都对应票面的一条要求）：
 *
 *  1. **与 completed / failed / interrupted 分开**：标题明说"不是失败"，事实行给出
 *     reason / consumed / limit / version / closeout——这五个值与 CLI
 *     `render_pause_block` 显示的是同一份事件真值（`lib/runBudget.ts` 是共用取值口径）。
 *  2. **没有权威的本地状态**：面板整体由 `conversation.run_paused` 投影驱动（不变量 #22）
 *     ——`run/resumed` 一到它自己消失；刷新/重放得到同一个投影。输入框里的草稿是**用户
 *     意图**（不是会话事实），所以由 App 持有（重读日志不会把它清掉）。
 *  3. **恢复请求的四个声明**都由投影给出：run_id / version / 绝对 ceiling（用户输入）/
 *     resume_basis=budget_increase（本票唯一合法值）。
 *  4. 被拒（409/422）时**不动**输入框内容：用户只需把数字改大再来一次。
 *
 *  纯展示组件：不 fetch、不改会话状态；`onResume` 由 App 转给 `useSession`。 */

import { PauseCircle } from 'lucide-react';
import type { RunPausedInfo } from '../types';
import {
  ceilingDraftError,
  CONTINUATION_SECTIONS,
  pauseFacts,
} from '../lib/runBudget';

export interface PausedPanelProps {
  paused: RunPausedInfo;
  /** 用户正在编辑的绝对 ceiling 草稿（App 持有：刷新/重读不丢输入）。 */
  ceilingDraft: string;
  onCeilingDraftChange: (value: string) => void;
  /** 提交恢复（App 调 useSession.resumePausedRun）。 */
  onResume: () => void;
  /** 恢复请求在途：按钮禁用 + 文案改为进行中（防重复提交 = 两个 CAS 请求）。 */
  resuming: boolean;
}

export function PausedPanel({
  paused,
  ceilingDraft,
  onCeilingDraftChange,
  onResume,
  resuming,
}: PausedPanelProps) {
  const facts = pauseFacts(paused);
  const draftError = ceilingDraftError(paused, ceilingDraft);
  const continuation = paused.continuation;

  return (
    <div className="pause-panel" role="status" aria-live="polite">
      <div className="pause-panel-head">
        <PauseCircle size={14} aria-hidden="true" />
        <span>
          已在预算到顶处暂停（{facts.dimensionLabel}）——同一 run 的非终态收口，不是失败
        </span>
      </div>
      <div className="pause-panel-facts">
        {/* 缺失数字一律说 unavailable / unlimited，**永不**用 0 顶替（`11 §6.1`，
            与 CLI `_pause_facts` 同一口径）。 */}
        原因 {paused.reason} · 已消耗 {facts.consumed} 轮 · 绝对 ceiling{' '}
        {facts.ceiling === null ? 'unlimited' : facts.ceiling}
        {facts.remaining === null ? '' : `（剩余 ${facts.remaining}）`} · 预算版本{' '}
        {facts.version} · 收口 {facts.closeoutSource}
        {facts.localFuseTurns === null
          ? ''
          : ` · local fuse ${facts.localFuseTurns}${
              facts.localFuseSource ? `（${facts.localFuseSource}）` : ''
            }`}
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
      <div className="pause-panel-resume">
        <label htmlFor="pause-resume-ceiling">绝对 ceiling</label>
        <input
          id="pause-resume-ceiling"
          type="text"
          inputMode="numeric"
          value={ceilingDraft}
          onChange={(e) => onCeilingDraftChange(e.target.value)}
          aria-label="恢复用的绝对 turn ceiling"
          disabled={resuming}
        />
        <button
          className="pause-resume-btn"
          onClick={onResume}
          disabled={resuming || draftError !== null}
          title="以同一 run_id 恢复：提交绝对值 ceiling 与当前预算版本（CAS）"
        >
          <PauseCircle size={14} aria-hidden="true" />
          {resuming ? '恢复中…' : '恢复同一 run'}
        </button>
        {draftError !== null && !resuming && (
          <span className="pause-panel-error">{draftError}</span>
        )}
        {draftError === null && !resuming && (
          <span className="pause-panel-hint">
            {`至少 ${facts.minResumeCeiling}（须大于已消耗 ${facts.consumed} + 1，否则恢复后立刻再次暂停）；已消耗不重置，恢复沿用同一 run_id`}
          </span>
        )}
      </div>
    </div>
  );
}
