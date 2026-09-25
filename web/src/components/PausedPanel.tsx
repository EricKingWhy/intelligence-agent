/** 预算暂停面板（`#312` T4 建，`#313` T5 扩到四维）——把一个**非终态**暂停的完整
 *  事实摊开，并给出唯一的恢复动作：抬高卡住那一维的绝对 ceiling，然后以**同一
 *  run_id** 续跑。
 *
 *  设计要点（每条都对应票面的一条要求）：
 *
 *  1. **与 completed / failed / interrupted 分开**：标题明说"不是失败"，事实行给出
 *     reason / 读数 / version / closeout——这些值与 CLI `render_pause_block` 显示的
 *     是同一份事件真值（`lib/runBudget.ts` 是共用取值口径）。
 *  2. **读数按维度报**：暂停可能落在 turns / requests / tokens / cost 任何一维上
 *     （`#313`），标题与恢复输入都指向**真正卡住的那一维**。只读 turns 会把"被
 *     requests 卡住"显示成"已消耗 0 轮 · 无上限"，那是在陈述一件没发生过的事——
 *     四维清单（`facts.dimensions`）保证另外三维的事实也在屏幕上。
 *  3. **没有权威的本地状态**：面板整体由 `conversation.run_paused` 投影驱动（不变量 #22）
 *     ——`run/resumed` 一到它自己消失；刷新/重放得到同一个投影。输入框里的草稿是**用户
 *     意图**（不是会话事实），所以由 App 持有（重读日志不会把它清掉）。
 *  4. **恢复请求的四个声明**都由投影给出：run_id / version / 绝对 ceiling（用户输入）/
 *     resume_basis=budget_increase（本票唯一合法值）；ceiling 落在哪一维由
 *     `facts.resumeTarget` 决定。
 *  5. 被拒（409/422）时**不动**输入框内容：用户只需把数字改大再来一次。
 *
 *  纯展示组件：不 fetch、不改会话状态；`onResume` 由 App 转给 `useSession`。 */

import { PauseCircle } from 'lucide-react';
import type { RunPausedInfo } from '../types';
import {
  ceilingDraftError,
  CONTINUATION_SECTIONS,
  pauseFacts,
  resumeInputHint,
  type DimensionFact,
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

/** 一维的读数行（与 CLI `_extra_dimension_lines` 同一口径的排版）。命中的那一维
 *  带"到顶"字样，用户一眼看到是谁卡住了。 */
function dimensionLine(fact: DimensionFact): string {
  const tripped = fact.tripped ? ' · 到顶' : '';
  return `${fact.consumedText} / limit ${fact.ceilingText}（剩余 ${fact.remainingText}）${tripped}`;
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
  // 标题按**命中的那一维**报读数：命中 local fuse（非 run 维）时没有 run 维读数可报，
  // 回落成维度标签（不把 turns 冒充成"卡住的那一维"）。
  const tripped = facts.tripped;
  const headline = tripped
    ? `${tripped.label}：已消耗 ${tripped.consumedText} · 绝对 ceiling ${tripped.ceilingText}${
        tripped.remaining === null ? '' : `（剩余 ${tripped.remainingText}）`
      }`
    : facts.dimensionLabel;

  return (
    <div className="pause-panel" role="status" aria-live="polite">
      <div className="pause-panel-head">
        <PauseCircle size={14} aria-hidden="true" />
        <span>已在预算到顶处暂停——同一 run 的非终态收口，不是失败</span>
      </div>
      <div className="pause-panel-facts">
        {/* 缺失数字一律说 unavailable / unlimited，**永不**用 0 顶替（`11 §6.1`，
            与 CLI `_pause_facts` 同一口径）。 */}
        原因 {paused.reason} · {headline} · 预算版本 {facts.version} · 收口{' '}
        {facts.closeoutSource}
        {facts.localFuseTurns === null
          ? ''
          : ` · local fuse ${facts.localFuseTurns}${
              facts.localFuseSource ? `（${facts.localFuseSource}）` : ''
            }`}
        {paused.trace_id ? ` · trace ${paused.trace_id}` : ''}
      </div>
      {/* 四维清单（`#313`）：只列有事实可说的维度——老暂停只有 turns，多打三行
          unavailable / unlimited 是噪声不是信息（与 CLI 同一取舍）。 */}
      <div className="pause-panel-facts">
        {facts.dimensions.map((fact) => (
          <div key={fact.spec.dimension}>
            {fact.spec.consumedKey}: {dimensionLine(fact)}
          </div>
        ))}
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
        <label htmlFor="pause-resume-ceiling">
          绝对 ceiling（{facts.resumeTarget.resumeField}）
        </label>
        <input
          id="pause-resume-ceiling"
          type="text"
          inputMode="decimal"
          value={ceilingDraft}
          onChange={(e) => onCeilingDraftChange(e.target.value)}
          aria-label={`恢复用的绝对 ceiling：${facts.resumeTarget.resumeField}`}
          disabled={resuming}
        />
        <button
          className="pause-resume-btn"
          onClick={onResume}
          disabled={resuming || draftError !== null}
          title={`以同一 run_id 恢复：提交 ${facts.resumeTarget.resumeField} 的绝对值与当前预算版本（CAS）`}
        >
          <PauseCircle size={14} aria-hidden="true" />
          {resuming ? '恢复中…' : '恢复同一 run'}
        </button>
        {draftError !== null && !resuming && (
          <span className="pause-panel-error">{draftError}</span>
        )}
        {draftError === null && !resuming && (
          <span className="pause-panel-hint">{resumeInputHint(facts)}</span>
        )}
      </div>
    </div>
  );
}
