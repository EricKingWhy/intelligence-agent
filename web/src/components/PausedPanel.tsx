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

/** 维度 key 的中文短名（暂停面板逐维清单的左侧标签）。 */
const DIMENSION_DISPLAY: Record<string, string> = {
  agent_turns: '轮数',
  model_requests: '模型请求',
  total_tokens: '字数',
  cost_usd: '成本',
};

/** 一维的读数行（与 CLI `_extra_dimension_lines` 同一口径的排版）。命中的那一维
 *  带"到顶"字样，用户一眼看到是谁卡住了。 */
function dimensionLine(fact: DimensionFact): string {
  const tripped = fact.tripped ? ' · 到顶' : '';
  return `${fact.consumedText} / 上限 ${fact.ceilingText}（剩余 ${fact.remainingText}）${tripped}`;
}

/** 一个工具配额的读数行（与 CLI `_tool_dimension_lines` 同一取舍）：两个 counter
 *  分开写——`calls` 是被接纳的逻辑调用数，`attempts` 含 retry，它们不是同一个量。 */
function toolQuotaLine(fact: ToolQuotaFact): string {
  const tripped = fact.tripped ? ' · 到顶' : '';
  return `已用 ${fact.callsText} 次调用 / ${fact.attemptsText} 次尝试 / 上限 ${
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
    ? `截止时间 ${facts.deadline ?? 'unavailable'} 已到：` +
      '到点后不再接受新的模型请求 / 工具调用 / 子任务；' +
      '正在进行的调用会按各自的超时时间正常结束'
    : tripped
      ? `${tripped.label}：已消耗 ${tripped.consumedText} · 上限 ${tripped.ceilingText}${
          tripped.remaining === null ? '' : `（剩余 ${tripped.remainingText}）`
        }`
      : trippedTool
      ? `${trippedTool.label}：已消耗 ${trippedTool.callsText} 次调用（${
          trippedTool.attemptsText
        } 次尝试） · 上限 ${trippedTool.ceilingText}${
          trippedTool.remaining === null ? '' : `（剩余 ${trippedTool.remainingText}）`
        }`
      : facts.dimensionLabel;

  return (
    <div className="pause-panel" role="status" aria-live="polite">
      <div className="pause-panel-head">
        <PauseCircle size={14} aria-hidden="true" />
        <span>
          {facts.deadlinePause
            ? '已在截止时间处暂停——这次任务只是暂停，不是出错'
            : '预算已用完，任务暂停了——不是出错'}
        </span>
      </div>
      <div className="pause-panel-facts">
        {/* 缺失数字一律说 unavailable / unlimited，**永不**用 0 顶替（`11 §6.1`，
            与 CLI `_pause_facts` 同一口径）。 */}
        原因：{facts.reasonLabel} · {headline} · 预算版本 {facts.version} · 结束方式{' '}
        {facts.closeoutSource}
        {facts.localFuseTurns === null
          ? ''
          : ` · 单次执行轮数上限 ${facts.localFuseTurns}${
              facts.localFuseSource ? `（${facts.localFuseSource}）` : ''
            }`}
        {paused.trace_id ? ` · trace ${paused.trace_id}` : ''}
      </div>
      {/* 四维清单（`#313`）：只列有事实可说的维度——老暂停只有 turns，多打三行
          unavailable / unlimited 是噪声不是信息（与 CLI 同一取舍）。 */}
      <div className="pause-panel-facts">
        {facts.dimensions.map((fact) => (
          <div key={fact.spec.dimension}>
            {DIMENSION_DISPLAY[fact.spec.consumedKey] ?? fact.spec.consumedKey}: {dimensionLine(fact)}
          </div>
        ))}
      </div>
      {/* per-tool 配额清单（`#314`）：配置过的 ∪ 调用过的，按工具名排序。两个 counter
          分开写（calls / attempts）——把 retry 当成新的逻辑调用是票面明令禁止的误报。 */}
      {facts.toolQuotas.length > 0 && (
        <div className="pause-panel-facts">
          {facts.toolQuotas.map((fact) => (
            <div key={fact.dimension}>
              工具 {fact.name}: {toolQuotaLine(fact)}
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
        {/* `#1197` 项 2：这里**没有** context 分支了——上下文满的暂停由 App 路由到
            `ContextFullDialog`（一句话 + 换模型/结束），不再走这个技术屏。原先
            `#1026` 那段"请手动 /compact、或调大 max_context_tokens"的引导连同
            `isContextPause` 判别一起删除：不可达的分支留着只会让下一个改这里的人
            以为面板还要负责 context 暂停。路由不变量由
            `App.contextFullRouting.test.tsx` 在 App 层钉住。 */}
        <label htmlFor="pause-resume-ceiling">
          {/* `#315`：deadline 的目标是**时刻**不是 ceiling 数字——标签与输入模式都要换，
              否则用户会按数字形状去填一个 RFC 3339 文本（或者反过来）。 */}
          {facts.deadlinePause
            ? `新的绝对截止时刻（${resumeTargetLabel(facts.resumeTarget)}，RFC 3339 UTC）`
            : `上限（${resumeTargetLabel(facts.resumeTarget)}）`}
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
              : `恢复用的上限：${resumeTargetLabel(facts.resumeTarget)}`
          }
          disabled={resuming}
        />
        <button
          className="pause-resume-btn"
          onClick={onResume}
          disabled={resuming || draftError !== null}
          title={`继续这次任务：提交 ${resumeTargetLabel(facts.resumeTarget)} 的数值与当前预算版本（若版本已变会提示冲突）`}
        >
          <PauseCircle size={14} aria-hidden="true" />
          {resuming ? '继续中…' : '继续这次任务'}
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
