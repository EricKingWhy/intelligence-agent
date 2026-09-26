/** 预算暂停面板（`#312` T4 建，`#313` T5 扩到四维，`#314` T6 加 per-tool 配额）——
 *  把一个**非终态**暂停的完整事实摊开，并给出唯一的恢复动作：抬高卡住那一维的绝对
 *  ceiling，然后以**同一 run_id** 续跑。
 *
 *  设计要点（每条都对应票面的一条要求）：
 *
 *  1. **与 completed / failed / interrupted 分开**：标题明说"不是失败"，事实行给出
 *     reason / 读数 / version / closeout——这些值与 CLI `render_pause_block` 显示的
 *     是同一份事件真值（`lib/runBudget.ts` 是共用取值口径）。
 *  2. **读数按维度报**：暂停可能落在 turns / requests / tokens / cost 任何一维上
 *     （`#313`），标题与恢复输入都指向**真正卡住的那一维**。只读 turns 会把"被
 *     requests 卡住"显示成"已消耗 0 轮 · 无上限"，那是在陈述一件没发生过的事——
 *     四维清单（`facts.dimensions`）保证另外三维的事实也在屏幕上。`#314` 的
 *     per-tool 配额同理：它有自己的清单（`facts.toolQuotas`），且**两个 counter
 *     分开显示**（calls = 被接纳的逻辑调用，attempts = 真实尝试含 retry）。
 *  3. **没有权威的本地状态**：面板整体由 `conversation.run_paused` 投影驱动（不变量 #22）
 *     ——`run/resumed` 一到它自己消失；刷新/重放得到同一个投影。输入框里的草稿是**用户
 *     意图**（不是会话事实），所以由 App 持有（重读日志不会把它清掉）。
 *  4. **恢复请求的四个声明**都由投影给出：run_id / version / 绝对 ceiling（用户输入）/
 *     resume_basis=budget_increase（本票唯一合法值）；ceiling 落在哪一维由
 *     `facts.resumeTarget` 决定（run 维给字段名，工具配额给工具名）。
 *  5. 被拒（409/422）时**不动**输入框内容：用户只需把数字改大再来一次。
 *
 *  纯展示组件：不 fetch、不改会话状态；`onResume` 由 App 转给 `useSession`。 */

import { PauseCircle } from 'lucide-react';
import type { RunPausedInfo } from '../types';
import {
  ceilingDraftError,
  CONTINUATION_SECTIONS,
  DEADLINE_DIMENSION,
  DEADLINE_EXAMPLE,
  pauseFacts,
  resumeInputHint,
  resumeTargetLabel,
  type DimensionFact,
  type ToolQuotaFact,
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

/** 一个工具配额的读数行（与 CLI `_tool_dimension_lines` 同一取舍）：两个 counter
 *  分开写——`calls` 是被接纳的逻辑调用数，`attempts` 含 retry，它们不是同一个量。 */
function toolQuotaLine(fact: ToolQuotaFact): string {
  const tripped = fact.tripped ? ' · 到顶' : '';
  return `consumed ${fact.callsText} calls / ${fact.attemptsText} attempts / limit ${
    fact.ceilingText
  }（剩余 ${fact.remainingText}）${tripped}`;
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
  // 标题按**命中的那一维**报读数：命中 local fuse（非 run 维）或某个**工具配额**
  // （`#314`）时没有 run 维读数可报，回落成维度标签（不把 turns 冒充成"卡住的那一维"）。
  const tripped = facts.tripped;
  const trippedTool = facts.trippedTool;
  // `#315`：deadline 暂停既没有"某一维的读数"也没有工具配额可报（到点判的是时刻，
  // 不是 consumed 与 ceiling 比大小）——标题报**时刻本身**与到点后的准入边界，
  // 回落成 `dimensionLabel` 会把"哪一维卡住"与"到点后不再接纳什么"两件事一起丢掉。
  // 括号里的原始维度名与另外两支同惯例（`run.max_agent_turns_total` /
  // `run.tool_call_limits.<name>` 都在标题里点名）：CLI 的暂停摘要也打
  // `dimension=run.deadline_at`，"卡在哪一维"不该只有命令行看得见。
  const headline = facts.deadlinePause
    ? `绝对截止时刻 ${facts.deadline ?? 'unavailable'} 已到（${DEADLINE_DIMENSION}）：` +
      '到点后不再接纳新的 Provider 请求 / 工具调用 / 子 Agent；' +
      '已在途的调用按各自的 timeout / Ledger 语义收尾'
    : tripped
      ? `${tripped.label}：已消耗 ${tripped.consumedText} · 绝对 ceiling ${tripped.ceilingText}${
          tripped.remaining === null ? '' : `（剩余 ${tripped.remainingText}）`
        }`
      : trippedTool
      ? `${trippedTool.label}：已消耗 ${trippedTool.callsText} 次调用（${
          trippedTool.attemptsText
        } 次尝试） · 绝对 ceiling ${trippedTool.ceilingText}${
          trippedTool.remaining === null ? '' : `（剩余 ${trippedTool.remainingText}）`
        }`
      : facts.dimensionLabel;

  return (
    <div className="pause-panel" role="status" aria-live="polite">
      <div className="pause-panel-head">
        <PauseCircle size={14} aria-hidden="true" />
        <span>
          {facts.deadlinePause
            ? '已在绝对截止时刻处暂停——同一 run 的非终态收口，不是失败'
            : '已在预算到顶处暂停——同一 run 的非终态收口，不是失败'}
        </span>
      </div>
      <div className="pause-panel-facts">
        {/* 缺失数字一律说 unavailable / unlimited，**永不**用 0 顶替（`11 §6.1`，
            与 CLI `_pause_facts` 同一口径）。 */}
        原因 {paused.reason}（{facts.reasonLabel}）· {headline} · 预算版本 {facts.version} · 收口{' '}
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
      {/* per-tool 配额清单（`#314`）：配置过的 ∪ 调用过的，按工具名排序。两个 counter
          分开写（calls / attempts）——把 retry 当成新的逻辑调用是票面明令禁止的误报。 */}
      {facts.toolQuotas.length > 0 && (
        <div className="pause-panel-facts">
          {facts.toolQuotas.map((fact) => (
            <div key={fact.dimension}>
              tool {fact.name}: {toolQuotaLine(fact)}
            </div>
          ))}
        </div>
      )}
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
          {/* `#315`：deadline 的目标是**时刻**不是 ceiling 数字——标签与输入模式都要换，
              否则用户会按数字形状去填一个 RFC 3339 文本（或者反过来）。 */}
          {facts.deadlinePause
            ? `新的绝对截止时刻（${resumeTargetLabel(facts.resumeTarget)}，RFC 3339 UTC）`
            : `绝对 ceiling（${resumeTargetLabel(facts.resumeTarget)}）`}
        </label>
        <input
          id="pause-resume-ceiling"
          type="text"
          inputMode={facts.deadlinePause ? 'text' : 'decimal'}
          placeholder={facts.deadlinePause ? DEADLINE_EXAMPLE : undefined}
          value={ceilingDraft}
          onChange={(e) => onCeilingDraftChange(e.target.value)}
          aria-label={
            facts.deadlinePause
              ? `恢复用的绝对截止时刻：${resumeTargetLabel(facts.resumeTarget)}（RFC 3339 UTC）`
              : `恢复用的绝对 ceiling：${resumeTargetLabel(facts.resumeTarget)}`
          }
          disabled={resuming}
        />
        <button
          className="pause-resume-btn"
          onClick={onResume}
          disabled={resuming || draftError !== null}
          title={`以同一 run_id 恢复：提交 ${resumeTargetLabel(facts.resumeTarget)} 的绝对值与当前预算版本（CAS）`}
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
